"""Reproduce a Windows reader denying replacement of a status JSON file."""
import ctypes
import json
import threading
import time
from pathlib import Path
from cv_camera_test import write_json

target = Path(__file__).resolve().parent/'digit_logs/status_lock_check.json'
target.parent.mkdir(exist_ok=True)
api = ctypes.WinDLL('kernel32',use_last_error=True)
api.CreateFileW.argtypes = [ctypes.c_wchar_p,ctypes.c_uint32,ctypes.c_uint32,
                           ctypes.c_void_p,ctypes.c_uint32,ctypes.c_uint32,ctypes.c_void_p]
api.CreateFileW.restype = ctypes.c_void_p
api.CloseHandle.argtypes = [ctypes.c_void_p]
api.CloseHandle.restype = ctypes.c_int


def reader_lock():
    handle = api.CreateFileW(str(target),0x80000000,1,None,3,0x80,None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return handle


write_json(target,{'value':1})
handle = reader_lock()
releaser = threading.Thread(target=lambda:(time.sleep(.15),api.CloseHandle(handle)))
releaser.start()
assert write_json(target,{'value':2}) is True
releaser.join()
assert json.loads(target.read_text())['value']==2
handle = reader_lock()
try:
    assert write_json(target,{'value':3},required=False) is False
finally:
    api.CloseHandle(handle)
assert json.loads(target.read_text())['value']==2
assert write_json(target,{'value':4},required=False) is True
assert json.loads(target.read_text())['value']==4
target.unlink()
print('Windows lock retry, optional write failure, and recovery passed.')
