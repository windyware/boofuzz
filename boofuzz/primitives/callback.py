from base_primitive import BasePrimitive

class CallBack(BasePrimitive):

    def __init__(self, callback):
        """

        :param callback: run this func when mutux this node
        """
        self._callback = callback
        pass

    @property
    def callback(self):
        return self._callback

    pass
