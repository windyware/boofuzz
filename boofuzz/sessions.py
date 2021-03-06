from __future__ import absolute_import

import cPickle
import logging
import re
import sys
import threading
import time
import zlib

from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop
from tornado.wsgi import WSGIContainer

from . import blocks
from . import event_hook
from . import fuzz_logger
from . import fuzz_logger_text
from . import ifuzz_logger
from . import pgraph
from . import primitives
from . import sex
from .web.app import app


class Target(object):
    """Target descriptor container.

    Takes an ITargetConnection and wraps send/recv with appropriate
    FuzzDataLogger calls.

    Encapsulates pedrpc connection logic.

    Contains a logger which is configured by Session.add_target().

    Example:
        tcp_target = Target(SocketConnection(host='127.0.0.1', port=17971))
    """

    def __init__(self, connection, procmon=None, procmon_options=None, netmon=None):
        """
        Args:
            connection (itarget_connection.ITargetConnection): Connection to system under test.
        """
        self._fuzz_data_logger = None

        self._target_connection = connection
        self.procmon = procmon
        self.netmon = netmon

        # set these manually once target is instantiated.
        self.vmcontrol = None
        self.netmon_options = {}
        if procmon_options is None:
            procmon_options = {}
        self.procmon_options = procmon_options
        self.vmcontrol_options = {}

    def close(self):
        """
        Close connection to the target.

        :return: None
        """
        self._target_connection.close()

    def open(self):
        """
        Opens connection to the target. Make sure to call close!

        :return: None
        """
        self._target_connection.open()

    def pedrpc_connect(self):
        """
        Pass specified target parameters to the PED-RPC server.
        """
        # If the process monitor is alive, set it's options
        if self.procmon:
            while 1:
                if self.procmon.alive():
                    break

                time.sleep(1)

            # connection established.
            for key, value in self.procmon_options.items():
                getattr(self.procmon, 'set_{0}'.format(key))(value)

        # If the network monitor is alive, set it's options
        if self.netmon:
            while 1:
                if self.netmon.alive():
                    break

                time.sleep(1)

            # connection established.
            for key in self.netmon_options.keys():
                eval('self.netmon.set_%s(self.netmon_options["%s"])' % (key, key))

    def recv(self, max_bytes):
        """
        Receive up to max_bytes data from the target.

        Args:
            max_bytes (int): Maximum number of bytes to receive.

        Returns:
            Received data.
        """
        if self._fuzz_data_logger is not None:
            self._fuzz_data_logger.log_info("Receiving...")

        data = self._target_connection.recv(max_bytes=max_bytes)

        if self._fuzz_data_logger is not None:
            self._fuzz_data_logger.log_recv(data)

        return data

    def send(self, data):
        """
        Send data to the target. Only valid after calling open!

        Args:
            data: Data to send.

        Returns:
            None
        """
        if self._fuzz_data_logger is not None:
            self._fuzz_data_logger.log_send(data)

        num_sent = self._target_connection.send(data=data)

        if self._fuzz_data_logger is not None:
            self._fuzz_data_logger.log_info("{0} bytes sent".format(num_sent))

    def set_fuzz_data_logger(self, fuzz_data_logger):
        """
        Set this object's fuzz data logger -- for sent and received fuzz data.

        :param fuzz_data_logger: New logger.
        :type fuzz_data_logger: ifuzz_logger.IFuzzLogger

        :return: None
        """
        self._fuzz_data_logger = fuzz_data_logger


class Connection(pgraph.Edge):
    def __init__(self, src, dst, callback=None):
        """
        Extends pgraph.edge with a callback option. This allows us to register a function to call between node
        transmissions to implement functionality such as challenge response systems. The callback method must follow
        this prototype::

            def callback(session, node, edge, sock)

        Where node is the node about to be sent, edge is the last edge along the current fuzz path to "node", session
        is a pointer to the session instance which is useful for snagging data such as sesson.last_recv which contains
        the data returned from the last socket transmission and sock is the live socket. A callback is also useful in
        situations where, for example, the size of the next packet is specified in the first packet.

        Args:
            src (int): Edge source ID
            dst (int): Edge destination ID
            callback (function): Optional. Callback function to pass received data to between node xmits
        """

        super(Connection, self).__init__(src, dst)

        self.callback = callback


class Session(pgraph.Graph):
    """
    Extends pgraph.graph and provides a container for architecting protocol dialogs.

    Args:
        session_filename (str): Filename to serialize persistent data to. Default None.
        skip (int):             Number of test cases to skip. Default 0.
        sleep_time (float):     Time in seconds to sleep in between tests. Default 0.
        restart_interval (int): Restart the target after n test cases, disable by setting to 0 (default).
        crash_threshold (int):  Maximum number of crashes allowed before a node is exhaust. Default 3.
        restart_sleep_time (int): Time in seconds to sleep when target can't be restarted. Default 5.
        web_port (int):         Port for monitoring fuzzing campaign via a web browser. Default 26000.
        fuzz_data_logger (fuzz_logger.FuzzLogger): For saving test data and results.. Default Log to STDOUT.
        check_data_received_each_request (bool): If True, Session will verify that some data has
                                                 been received after transmitting each node, and if not, register a
                                                 failure. If False, this check will not be performed. Default True.
        ignore_connection_reset (bool): Log ECONNRESET errors ("Target connection reset") as "info" instead of
                                failures.
        ignore_connection_aborted (bool): Log ECONNABORTED errors as "info" instead of failures.
        target (Target):        Target for fuzz session. Target must be fully initialized. Default None.

        log_level (int):        DEPRECATED Unused. Logger settings are now configured in fuzz_data_logger.
                                Was once used to set the log level.
        logfile (str):          DEPRECATED Unused. Logger settings are now configured in fuzz_data_logger.
                                Was once the name of the log file.
        logfile_level (int):    DEPRECATED Unused. Logger settings are now configured in fuzz_data_logger.
                                Was once used to set the log level for the logfile. Default logger.INFO.
    """

    def __init__(self, session_filename=None, skip=0, sleep_time=0.0, restart_interval=0, web_port=26000,
                 crash_threshold=3, restart_sleep_time=5, fuzz_data_logger=None,
                 check_data_received_each_request=True,
                 log_level=logging.INFO, logfile=None, logfile_level=logging.DEBUG,
                 ignore_connection_reset=False,
                 ignore_connection_aborted=False,
                 target=None,
                 ):
        self._ignore_connection_reset = ignore_connection_reset
        self._ignore_connection_aborted = ignore_connection_aborted
        _ = log_level
        _ = logfile
        _ = logfile_level

        super(Session, self).__init__()

        self.session_filename = session_filename
        self.skip = skip
        self.sleep_time = sleep_time
        self.restart_interval = restart_interval
        self.web_port = web_port
        self.crash_threshold = crash_threshold
        self.restart_sleep_time = restart_sleep_time
        if fuzz_data_logger is None:
            self._fuzz_data_logger = fuzz_logger.FuzzLogger(fuzz_loggers=[fuzz_logger_text.FuzzLoggerText()])
        else:
            self._fuzz_data_logger = fuzz_data_logger
        self._check_data_received_each_request = check_data_received_each_request
        # Flag used to cancel fuzzing for a given primitive:
        self._skip_after_cur_test_case = False

        self.web_interface_thread = self.build_webapp_thread(port=self.web_port)

        self.total_num_mutations = 0
        self.total_mutant_index = 0
        self.fuzz_node = None
        self.targets = []
        self.netmon_results = {}
        self.procmon_results = {}
        self.is_paused = False
        self.crashing_primitives = {}
        self.on_failure = event_hook.EventHook()

        # import settings if they exist.
        self.import_file()

        # create a root node. we do this because we need to start fuzzing from a single point and the user may want
        # to specify a number of initial requests.
        self.root = pgraph.Node()
        self.root.name = "__ROOT_NODE__"
        self.root.label = self.root.name
        self.last_recv = None
        self.last_send = None

        self.add_node(self.root)

        if target is not None:
            self.add_target(target=target)

    def add_node(self, node):
        """
        Add a pgraph node to the graph. We overload this routine to automatically generate and assign an ID whenever a
        node is added.

        Args:
            node (pgraph.Node): Node to add to session graph
        """

        node.number = len(self.nodes)
        node.id = len(self.nodes)

        if node.id not in self.nodes:
            self.nodes[node.id] = node

        return self

    def add_target(self, target):
        """
        Add a target to the session. Multiple targets can be added for parallel fuzzing.

        Args:
            target (Target): Target to add to session
        """

        # pass specified target parameters to the PED-RPC server.
        target.pedrpc_connect()
        target.set_fuzz_data_logger(fuzz_data_logger=self._fuzz_data_logger)

        # add target to internal list.
        self.targets.append(target)

    def connect(self, src, dst=None, callback=None):
        """
        Create a connection between the two requests (nodes) and register an optional callback to process in between
        transmissions of the source and destination request. Leverage this functionality to handle situations such as
        challenge response systems. The session class maintains a top level node that all initial requests must be
        connected to. Example::

            sess = sessions.session()
            sess.connect(sess.root, s_get("HTTP"))

        If given only a single parameter, sess.connect() will default to attaching the supplied node to the root node.
        This is a convenient alias and is identical to the second line from the above example::

            sess.connect(s_get("HTTP"))

        If you register callback method, it must follow this prototype::

            def callback(session, node, edge, sock)

        Where node is the node about to be sent, edge is the last edge along the current fuzz path to "node", session
        is a pointer to the session instance which is useful for snagging data such as session.last_recv which contains
        the data returned from the last socket transmission and sock is the live socket. A callback is also useful in
        situations where, for example, the size of the next packet is specified in the first packet. As another
        example, if you need to fill in the dynamic IP address of the target register a callback that snags the IP
        from sock.getpeername()[0].

        Args:
            src (str or Request (pgrah.Node)): Source request name or request node
            dst (str or Request (pgrah.Node), optional): Destination request name or request node
            callback (def, optional): Callback function to pass received data to between node xmits. Default None.

        Returns:
            pgraph.Edge: The edge between the src and dst.
        """

        # if only a source was provided, then make it the destination and set the source to the root node.
        if dst is None:
            dst = src
            src = self.root

        # if source or destination is a name, resolve the actual node.
        if type(src) is str:
            src = self.find_node("name", src)

        if type(dst) is str:
            dst = self.find_node("name", dst)

        # if source or destination is not in the graph, add it.
        if src != self.root and not self.find_node("name", src.name):
            self.add_node(src)

        if self.find_node("name", dst.name) is None:
            self.add_node(dst)

        # create an edge between the two nodes and add it to the graph.
        edge = Connection(src.id, dst.id, callback)
        self.add_edge(edge)

        return edge

    def export_file(self):
        """
        Dump various object values to disk.

        @see: import_file()
        """

        if not self.session_filename:
            return

        data = {
            "session_filename": self.session_filename,
            "skip": self.total_mutant_index,
            "sleep_time": self.sleep_time,
            "restart_sleep_time": self.restart_sleep_time,
            "restart_interval": self.restart_interval,
            "web_port": self.web_port,
            "crash_threshold": self.crash_threshold,
            "total_num_mutations": self.total_num_mutations,
            "total_mutant_index": self.total_mutant_index,
            "netmon_results": self.netmon_results,
            "procmon_results": self.procmon_results,
            "is_paused": self.is_paused
        }

        fh = open(self.session_filename, "wb+")
        fh.write(zlib.compress(cPickle.dumps(data, protocol=2)))
        fh.close()

    def fuzz(self):
        """Fuzz the entire protocol tree.

        Iterates through and fuzzes all fuzz cases, skipping according to
        self.skip and restarting based on self.restart_interval.

        If you want the web server to be available, your program must persist
        after calling this method. helpers.pause_for_signal() is
        available to this end.

        Returns:
            None
        """
        self.total_mutant_index = 0
        self.total_num_mutations = self.num_mutations()

        self._main_fuzz_loop(self._iterate_protocol())

    def fuzz_single_node_by_path(self, node_names):
        """Fuzz a particular node via the path in node_names.

        Args:
            node_names (list of str): List of node names leading to target.
        """
        node_edges = self._path_names_to_edges(node_names=node_names)

        self.total_mutant_index = 0
        self.total_num_mutations = self.nodes[node_edges[-1].dst].num_mutations()

        self._main_fuzz_loop(self._iterate_single_node(node_edges))

    def fuzz_by_name(self, name):
        """Fuzz a particular test case or node by name.

        Args:
            name (str): Name of node.
        """
        self.fuzz_single_node_by_path(re.split('->', name))

    def fuzz_single_case(self, mutant_index):
        """Fuzz a test case by mutant_index.

        Args:
            mutant_index (int): Positive non-zero integer.

        Returns:
            None

        Raises:
            sex.SulleyRuntimeError: If any error is encountered while executing the test case.
        """
        self.total_mutant_index = 0
        self.total_num_mutations = 1

        self._main_fuzz_loop(self._iterate_single_case_by_index(mutant_index))

    def _main_fuzz_loop(self, fuzz_case_iterator):
        """Execute main fuzz logic; takes an iterator of test cases.

        Preconditions: `self.total_mutant_index` and `self.total_num_mutations` are set properly.

        Args:
            fuzz_case_iterator (Iterable): An iterator that walks through fuzz cases.

        Returns:
            None
        """

        # web
        self.server_init()

        try:
            num_cases_actually_fuzzed = 0
            for fuzz_args in fuzz_case_iterator:
                # skip until we pass self.skip
                if self.total_mutant_index <= self.skip:
                    continue

                # Check restart interval
                if num_cases_actually_fuzzed \
                        and self.restart_interval \
                        and num_cases_actually_fuzzed % self.restart_interval == 0:
                    self._fuzz_data_logger.open_test_step("restart interval of %d reached" % self.restart_interval)
                    self.restart_target(self.targets[0])

                self._fuzz_current_case(*fuzz_args)

                num_cases_actually_fuzzed += 1
        except KeyboardInterrupt:
            # TODO: should wait for the end of the ongoing test case, and stop gracefully netmon and procmon
            self.export_file()
            self._fuzz_data_logger.log_error("SIGINT received ... exiting")
            raise
        except sex.BoofuzzRestartFailedError:
            self._fuzz_data_logger.log_error("Restarting the target failed, exiting.")
            self.export_file()
            raise
        except sex.BoofuzzTargetConnectionFailedError:
            self._fuzz_data_logger.log_error(
                "Cannot connect to target; target presumed down."
                " Note: Normally a failure should be detected, and the target reset."
                " This error may mean you have no restart method configured, or your error"
                " detection is not working.")
            self.export_file()

    def import_file(self):
        """
        Load various object values from disk.

        @see: export_file()
        """
        if self.session_filename is None:
            return

        try:
            with open(self.session_filename, "rb") as f:
                data = cPickle.loads(zlib.decompress(f.read()))
        except (IOError, zlib.error, cPickle.UnpicklingError):
            return

        # update the skip variable to pick up fuzzing from last test case.
        self.skip = data["total_mutant_index"]
        self.session_filename = data["session_filename"]
        self.sleep_time = data["sleep_time"]
        self.restart_sleep_time = data["restart_sleep_time"]
        self.restart_interval = data["restart_interval"]
        self.web_port = data["web_port"]
        self.crash_threshold = data["crash_threshold"]
        self.total_num_mutations = data["total_num_mutations"]
        self.total_mutant_index = data["total_mutant_index"]
        self.netmon_results = data["netmon_results"]
        self.procmon_results = data["procmon_results"]
        self.is_paused = data["is_paused"]

    # noinspection PyMethodMayBeStatic
    def log(self, msg, level=1):
        raise Exception("Depreciated!")

    def num_mutations(self, this_node=None, path=()):
        """
        Number of total mutations in the graph. The logic of this routine is identical to that of fuzz(). See fuzz()
        for inline comments. The member variable self.total_num_mutations is updated appropriately by this routine.

        Args:
            this_node (request (node)): Current node that is being fuzzed. Default None.
            path (list): Nodes along the path to the current one being fuzzed. Default [].

        Returns:
            int: Total number of mutations in this session.
        """

        if this_node is None:
            this_node = self.root
            self.total_num_mutations = 0

        if isinstance(path, tuple):
            path = list(path)

        for edge in self.edges_from(this_node.id):
            next_node = self.nodes[edge.dst]
            self.total_num_mutations += next_node.num_mutations()

            if edge.src != self.root.id:
                path.append(edge)

            self.num_mutations(next_node, path)

        # finished with the last node on the path, pop it off the path stack.
        if path:
            path.pop()

        return self.total_num_mutations

    def pause(self):
        """
        If that pause flag is raised, enter an endless loop until it is lowered.
        """
        while 1:
            if self.is_paused:
                time.sleep(1)
            else:
                break

    def poll_pedrpc(self, target):
        """
        Poll the PED-RPC endpoints (netmon, procmon etc...) for the target.

        Args:
            target (Target): Session target whose PED-RPC services we are polling
        """
        # kill the pcap thread and see how many bytes the sniffer recorded.
        if target.netmon:
            captured_bytes = target.netmon.post_send()
            self._fuzz_data_logger.log_info(
                "netmon captured %d bytes for test case #%d" % (captured_bytes, self.total_mutant_index))
            self.netmon_results[self.total_mutant_index] = captured_bytes

        # check if our fuzz crashed the target. procmon.post_send() returns False if the target crashes.
        if target.procmon:
            self._fuzz_data_logger.open_test_step("Contact process monitor")
            self._fuzz_data_logger.log_check("procmon.post_send()")
            if target.procmon.post_send():
                self._fuzz_data_logger.log_pass("No crash detected.")
            else:
                self._fuzz_data_logger.log_fail(
                    "procmon detected crash on test case #{0}: {1}".format(self.total_mutant_index,
                                                                           target.procmon.get_crash_synopsis()))

    def _process_failures(self, target):
        """Process any failure sin self.crash_synopses.

        If self.crash_synopses contains any entries, perform these failure-related actions:
         - log failure summary if needed
         - save failures to self.procmon_results (for website)
         - exhaust node if crash threshold is reached
         - target restart

        Should be called after each fuzz test case.

        Args:
            target (Target): Target to restart if failure occurred.

        Returns:
            None
        """
        crash_synopses = self._fuzz_data_logger.failed_test_cases.get(self.total_mutant_index, [])
        if len(crash_synopses) > 0:
            self._fuzz_data_logger.open_test_step("Failure summary")

            # retrieve the primitive that caused the crash and increment it's individual crash count.
            self.crashing_primitives[self.fuzz_node.mutant] = self.crashing_primitives.get(self.fuzz_node.mutant, 0) + 1

            # print crash synopsis
            if len(crash_synopses) > 1:
                # Prepend a header if > 1 failure report, so that they are visible from the main web page
                synopsis = "({0} reports) {1}".format(len(crash_synopses), "\n".join(crash_synopses))
            else:
                synopsis = "\n".join(crash_synopses)
            self.procmon_results[self.total_mutant_index] = synopsis
            self._fuzz_data_logger.log_info(self.procmon_results[self.total_mutant_index].split("\n")[0])

            # if the user-supplied crash threshold is reached, exhaust this node.
            if self.crashing_primitives[self.fuzz_node.mutant] >= self.crash_threshold:
                # as long as we're not a group and not a repeat.
                if not isinstance(self.fuzz_node.mutant, primitives.Group):
                    if not isinstance(self.fuzz_node.mutant, blocks.Repeat):
                        skipped = self.fuzz_node.mutant.num_mutations() - self.fuzz_node.mutant.mutant_index
                        self._skip_after_cur_test_case = True
                        self._fuzz_data_logger.open_test_step(
                            "Crash threshold reached for this primitive, exhausting %d mutants." % skipped
                        )
                        self.total_mutant_index += skipped
                        self.fuzz_node.mutant_index += skipped

            self.restart_target(target)

    # noinspection PyUnusedLocal
    def post_send(self, target, fuzz_data_logger, session, sock, *args, **kwargs):
        """
        Overload or replace this routine to specify actions to run after to each fuzz request. The order of events is
        as follows::

            pre_send() - req - callback ... req - callback - post_send()

        Potential uses:
         * Closing down a connection.
         * Checking for expected responses.

        @see: pre_send()

        Args:
            target (Target): Target with sock-like interface.
            fuzz_data_logger (ifuzz_logger.IFuzzLogger): Allows logging of test checks and passes/failures.
                Provided with a test case and test step already opened.

            session (Session): Session object calling post_send.
                Useful properties include last_send and last_recv.

            sock: DEPRECATED Included for backward-compatibility. Same as target.
            args: Implementations should include \*args and \**kwargs for forward-compatibility.
            kwargs: Implementations should include \*args and \**kwargs for forward-compatibility.
        """

        # default to doing nothing.
        self._fuzz_data_logger.log_info("No post_send callback registered.")

    # noinspection PyMethodMayBeStatic
    def pre_send(self, sock):
        """
        Overload or replace this routine to specify actions to run prior to each fuzz request. The order of events is
        as follows::

            pre_send() - req - callback ... req - callback - post_send()

        When fuzzing RPC for example, register this method to establish the RPC bind.

        @see: pre_send()

        Args:
            sock (Socket): Connected socket to target
        """

        # default to doing nothing.
        pass

    def restart_target(self, target):
        """
        Restart the fuzz target. If a VMControl is available revert the snapshot, if a process monitor is available
        restart the target process. Otherwise, do nothing.

        Args:
            target (session.target): Target we are restarting

        @raise sex.BoofuzzRestartFailedError if restart fails.
        """

        self._fuzz_data_logger.open_test_step("restarting target")
        if len(self.on_failure) > 0:
            for f in self.on_failure:
                self._fuzz_data_logger.open_test_step("calling registered on_failure method")
                f(logger=self._fuzz_data_logger)
        # vm restarting is the preferred method so try that before procmon.
        elif target.vmcontrol:
            self._fuzz_data_logger.log_info("restarting target virtual machine")
            target.vmcontrol.restart_target()

        # if we have a connected process monitor, restart the target process.
        elif target.procmon:
            self._fuzz_data_logger.log_info("restarting target process")

            if not target.procmon.restart_target():
                raise sex.BoofuzzRestartFailedError()

            self._fuzz_data_logger.log_info("giving the process 3 seconds to settle in ")
            time.sleep(3)

        # otherwise all we can do is wait a while for the target to recover on its own.
        else:
            self._fuzz_data_logger.log_info(
                "no reset handler available... sleeping for %d seconds" % self.restart_sleep_time
            )
            time.sleep(self.restart_sleep_time)

        # pass specified target parameters to the PED-RPC server to re-establish connections.
        target.pedrpc_connect()

    def server_init(self):
        """Called by fuzz() to initialize variables, web interface, etc.
        """
        if not self.web_interface_thread.isAlive():
            # spawn the web interface.
            self.web_interface_thread.start()

    def transmit(self, sock, node, edge):
        """
        Render and transmit a node, process callbacks accordingly.

        Args:
            sock (Target, optional): Socket-like object on which to transmit node
            node (pgraph.node.node (Node), optional): Request/Node to transmit
            edge (pgraph.edge.edge (pgraph.edge), optional): Edge along the current fuzz path from "node" to next node.
        """

        data = None

        # if the edge has a callback, process it. the callback has the option to render the node, modify it and return.
        if edge.callback:
            data = edge.callback(self, node, edge, sock)

        # if no data was returned by the callback, render the node here.
        if not data:
            data = node.render()

        try:
            # Try to send payload down-range
            self.targets[0].send(data)
            self.last_send = data

            if self._check_data_received_each_request:
                # Receive data
                # TODO: Remove magic number (10000)
                self.last_recv = self.targets[0].recv(10000)
                node.callback(self.last_recv)
                self._fuzz_data_logger.log_check("Verify some data was received from the target.")

                if not self.last_recv:
                    # Assume a crash?
                    self._fuzz_data_logger.log_fail("Nothing received from target.")
                else:

                    self._fuzz_data_logger.log_pass("Some data received from target.")
        except sex.BoofuzzTargetConnectionReset:
            if self._ignore_connection_reset:
                self._fuzz_data_logger.log_info("Target connection reset.")
            else:
                self._fuzz_data_logger.log_fail("Target connection reset.")
        except sex.BoofuzzTargetConnectionAborted as e:
            if self._ignore_connection_aborted:
                self._fuzz_data_logger.log_info("Target connection lost (socket error: {0} {1}): You may have a "
                                                "network issue, or an issue with firewalls or anti-virus. Try "
                                                "disabling your firewall."
                                                .format(e.socket_errno, e.socket_errmsg))
            else:
                self._fuzz_data_logger.log_fail("Target connection lost (socket error: {0} {1}): You may have a "
                                                "network issue, or an issue with firewalls or anti-virus. Try "
                                                "disabling your firewall."
                                                .format(e.socket_errno, e.socket_errmsg))
            pass

    def build_webapp_thread(self, port=26000):
        app.session = self
        http_server = HTTPServer(WSGIContainer(app))
        http_server.listen(port)
        flask_thread = threading.Thread(target=IOLoop.instance().start)
        flask_thread.daemon = True
        return flask_thread

    def _iterate_protocol(self):
        """
        Iterates over fuzz cases and mutates appropriately.
        On each iteration, one may call fuzz_current_case to do the
        actual fuzzing.

        :raise sex.SullyRuntimeError:
        """
        # we can't fuzz if we don't have at least one target and one request.
        if not self.targets:
            raise sex.SullyRuntimeError("No targets specified in session")

        if not self.edges_from(self.root.id):
            raise sex.SullyRuntimeError("No requests specified in session")

        self._reset_fuzz_state()

        for x in self._iterate_protocol_recursive(this_node=self.root, path=[]):
            yield x

    def _iterate_protocol_recursive(self, this_node, path):
        """
        Recursively iterates over fuzz nodes. Used by _fuzz_case_iterator.

        Args:
            this_node (node.Node): Current node that is being fuzzed.
            path (list of Connection): List of edges along the path to the current one being fuzzed.

        :raise sex.SullyRuntimeError:
        """
        # step through every edge from the current node.
        for edge in self.edges_from(this_node.id):
            # keep track of the path as we fuzz through it, don't count the root node.
            # we keep track of edges as opposed to nodes because if there is more then one path through a set of
            # given nodes we don't want any ambiguity.
            path.append(edge)

            for x in self._iterate_single_node(path):
                yield x

            # recursively fuzz the remainder of the nodes in the session graph.
            for x in self._iterate_protocol_recursive(self.fuzz_node, path):
                yield x

        # finished with the last node on the path, pop it off the path stack.
        if path:
            path.pop()

    def _iterate_single_node(self, path):
        """Iterate fuzz cases for the last node in path.

        Args:
            path (list of Connection): Nodes along the path to the current one being fuzzed.

        Raises:
            sex.SullyRuntimeError:
        """
        self.fuzz_node = self.nodes[path[-1].dst]
        # Loop through and yield all possible mutations of the fuzz node.
        # Note: when mutate() returns False, the node has been reverted to the default (valid) state.
        while self.fuzz_node.mutate():
            self.total_mutant_index += 1
            yield (path,)

            if self._skip_after_cur_test_case:
                self._skip_after_cur_test_case = False
                break
        self.fuzz_node.reset()

    def _iterate_single_case_by_index(self, test_case_index):
        fuzz_index = 1
        for fuzz_args in self._iterate_protocol():
            if fuzz_index >= test_case_index:
                self.total_mutant_index = 1
                yield fuzz_args
                break
            fuzz_index += 1

    def _path_names_to_edges(self, node_names):
        """Take a list of node names and return a list of edges describing that path.

        Args:
            node_names (list of str): List of node names describing a path.

        Returns:
            list of Connection: List of edges describing the path in node_names.
        """
        cur_node = self.root
        edge_path = []
        for node_name in node_names:
            next_node = None
            for edge in self.edges_from(cur_node.id):
                if self.nodes[edge.dst].name == node_name:
                    edge_path.append(edge)
                    next_node = self.nodes[edge.dst]
                    break
            if next_node is None:
                raise Exception("No edge found from {0} to {1}".format(cur_node.name, node_name))
            else:
                cur_node = next_node
        return edge_path

    def _fuzz_current_case(self, path):
        """
        Fuzzes the current test case. Current test case is controlled by
        fuzz_case_iterator().

        Args:
            path(list of Connection): Path to take to get to the target node.

        """
        target = self.targets[0]

        self.pause()  # only pauses conditionally

        message_path = "->".join([self.nodes[e.dst].name for e in path])

        if self.fuzz_node.mutant.name:
            primitive_under_test = self.fuzz_node.mutant.name
        else:
            primitive_under_test = 'no-name'

        test_case_name = "{0}.{1}.{2}".format(message_path, primitive_under_test, self.fuzz_node.mutant_index)

        self._fuzz_data_logger.open_test_case("{0}: {1}".format(self.total_mutant_index, test_case_name))

        self._fuzz_data_logger.log_info(
            "Type: %s. Default value: %s. Case %d of %d overall." % (
                type(self.fuzz_node.mutant).__name__,
                self.fuzz_node.mutant.original_value,
                self.total_mutant_index,
                self.total_num_mutations))

        if target.procmon:
            target.procmon.pre_send(self.total_mutant_index)

        if target.netmon:
            target.netmon.pre_send(self.total_mutant_index)

        target.open()

        self.pre_send(target)

        for e in path[:-1]:
            node = self.nodes[e.dst]
            self._fuzz_data_logger.open_test_step("Prep Node '{0}'".format(node.name))
            self.transmit(target, node, e)

        self._fuzz_data_logger.open_test_step("Fuzzing Node '{0}'".format(self.fuzz_node.name))
        self.transmit(target, self.fuzz_node, path[-1])

        self._fuzz_data_logger.open_test_step("Calling post_send function:")
        try:
            self.post_send(target=target, fuzz_data_logger=self._fuzz_data_logger, session=self, sock=target)
        except Exception as e:
            raise sex.BoofuzzError("Custom post_send method raised uncaught Exception.", e), None, sys.exc_info()[2]

        target.close()

        self._fuzz_data_logger.open_test_step("Sleep between tests.")
        self._fuzz_data_logger.log_info("sleeping for %f seconds" % self.sleep_time)
        time.sleep(self.sleep_time)

        self.poll_pedrpc(target)

        self._process_failures(target=target)

        self.export_file()

    def _reset_fuzz_state(self):
        """
        Restart the object's fuzz state.

        :return: None
        """
        self.total_mutant_index = 0
        if self.fuzz_node:
            self.fuzz_node.reset()
