from base_primitive import BasePrimitive
from ..blocks import *

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

        self._fuzzable = False
        self._mutant_index = 0  # current mutation index into the fuzz library.
        self._original_value = None  # original value of primitive.
        self._original_value_rendered = None  # original value as rendered

        self._fuzz_complete = False  # this flag is raised when the mutations are exhausted.
        self._fuzz_library = []  # library of static fuzz heuristics to cycle through.
        self._rendered = ""  # rendered value of primitive.
        self._value = None  # current value of primitive.

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
            _rendered += KEYS[self._key]
            _rendered += '\n'
        else:
            _rendered = self._callback(self._key, KEYS[self._key])

        return _rendered

    @property
    def key(self):
        return self._key


    pass



