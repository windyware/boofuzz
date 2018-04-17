from base_primitive import BasePrimitive
from .. import blocks

class PreElement(BasePrimitive):
    """
    Pre Elements that need to use in the current node


    """

    def __init__(self, key, callback = None):
        """

        :return:
        """
        self._key = key
        self._callback = callback

        pass

    def _render(self, value):
        """
        return
        :param value:
        :return:
        """

        if self._callback is None:
            _rendered  = self._key
            _rendered += ':'
            _rendered += blocks.KEYS[self._key]
            _rendered += '\n'
        else:
            self._callback(self._key, blocks.KEYS[self._key])

        return _rendered

    @property
    def key(self):
        return self._key


    pass



