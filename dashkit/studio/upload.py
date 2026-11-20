"""옮김: dashkit/project/upload.py(설계서 10-2). 기존 import 경로를 위해 같은 모듈 객체를 다시 내보낸다."""
import sys

from ..project import upload as _module

sys.modules[__name__] = _module
