from base_primitive import BasePrimitive

class CallBack(BasePrimitive):

    def __init__(self, callback):
        """
        :param callback: run this func when mutux this
         node
        """
        self._callback = callback
        self._fuzzable = False
        self._mutant_index = 0  # current mutation index into the fuzz library.
        self._original_value = None  # original value of primitive.
        self._original_value_rendered = None  # original value as rendered

        self._fuzz_complete = False  # this flag is raised when the mutations are exhausted.
        self._fuzz_library = []  # library of static fuzz heuristics to cycle through.
        self._rendered = ""  # rendered value of primitive.
        self._value = None  # current value of primitive.

    def _render(self, value):
        """
        return
        :param value:
        :return:
        """

        return  ''


    @property
    def callback(self):
        return self._callback

    pass
