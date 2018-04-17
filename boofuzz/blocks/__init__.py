# Import blocks at this level for backwards compatibility.
# blocks/ used to be blocks.py
from .block import Block
from .checksum import Checksum
from .repeat import Repeat
from .request import Request
from .size import Size

REQUESTS = {}
CURRENT = None

# add a global quene
# this is a dic that contact the data when fuzz one node,
# and i will send it to next node
KEYS = {}
