# Copyright (c) 2019, NVIDIA CORPORATION.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import ctypes
from enum import IntEnum

from numba import cuda
from numba.cuda import HostOnlyCUDAMemoryManager, IpcHandle, MemoryPointer

import rmm._lib as librmm


# Utility Functions
class RMMError(Exception):
    def __init__(self, errcode, msg):
        self.errcode = errcode
        super(RMMError, self).__init__(msg)


class rmm_allocation_mode(IntEnum):
    CudaDefaultAllocation = (0,)
    PoolAllocation = (1,)
    CudaManagedMemory = (2,)


# API Functions
def _initialize(
    pool_allocator=False,
    managed_memory=False,
    initial_pool_size=None,
    devices=0,
    logging=False,
):
    """
    Initializes RMM library using the options passed
    """
    allocation_mode = 0

    if pool_allocator:
        allocation_mode |= rmm_allocation_mode.PoolAllocation
    if managed_memory:
        allocation_mode |= rmm_allocation_mode.CudaManagedMemory

    if not pool_allocator:
        initial_pool_size = 0
    elif pool_allocator and initial_pool_size is None:
        initial_pool_size = 0
    elif pool_allocator and initial_pool_size == 0:
        initial_pool_size = 1

    if devices is None:
        devices = [0]
    elif isinstance(devices, int):
        devices = [devices]

    return librmm.rmm_initialize(
        allocation_mode, initial_pool_size, devices, logging
    )


def _finalize():
    """
    Finalizes the RMM library, freeing all allocated memory
    """
    return librmm.rmm_finalize()


def reinitialize(
    pool_allocator=False,
    managed_memory=False,
    initial_pool_size=None,
    devices=0,
    logging=False,
):
    """
    Finalizes and then initializes RMM using the options passed. Using memory
    from a previous initialization of RMM is undefined behavior and should be
    avoided.

    Parameters
    ----------
    pool_allocator : bool, default False
        If True, use a pool allocation strategy which can greatly improve
        performance.
    managed_memory : bool, default False
        If True, use managed memory for device memory allocation
    initial_pool_size : int, default None
        When `pool_allocator` is True, this indicates the initial pool size in
        bytes. None is used to indicate the default size of the underlying
        memorypool implementation, which currently is 1/2 total GPU memory.
    devices : int or List[int], default 0
        GPU device  IDs to register. By default registers only GPU 0.
    logging : bool, default False
        If True, enable run-time logging of all memory events
        (alloc, free, realloc).
        This has significant performance impact.
    """
    _finalize()
    return _initialize(
        pool_allocator=pool_allocator,
        managed_memory=managed_memory,
        initial_pool_size=initial_pool_size,
        devices=devices,
        logging=logging,
    )


def is_initialized():
    """
    Returns true if RMM has been initialized, false otherwise
    """
    return librmm.rmm_is_initialized()


def csv_log():
    """
    Returns a CSV log of all events logged by RMM, if logging is enabled
    """
    return librmm.rmm_csv_log()


def get_info(stream=0):
    """
    Get the free and total bytes of memory managed by a manager associated with
    the stream as a namedtuple with members `free` and `total`.
    """
    return librmm.rmm_getinfo(stream)


class RMMNumbaManager(HostOnlyCUDAMemoryManager):
    """
    External Memory Management Plugin implementation for Numba. Provides
    on-device allocation only.

    See http://numba.pydata.org/numba-doc/latest/cuda/external-memory.html for
    details of the interface being implemented here.
    """

    def initialize(self):
        # No special initialization needed to use RMM within a given context.
        pass

    def memalloc(self, size):
        """
        Allocate an on-device array from the RMM pool.
        """
        buf = librmm.DeviceBuffer(size=size)
        ctx = self.context
        ptr = ctypes.c_uint64(int(buf.ptr))
        finalizer = _make_finalizer(ptr.value, self.allocations)

        # self.allocations is initialized by the parent, HostOnlyCUDAManager,
        # and cleared upon context reset, so although we insert into it here
        # and delete from it in the finalizer, we need not do any other
        # housekeeping elsewhere.
        self.allocations[ptr.value] = buf

        return cuda.MemoryPointer(ctx, ptr, size, finalizer=finalizer)

    def get_ipc_handle(self, memory):
        """
        Get an IPC handle for the MemoryPointer memory with offset modified by
        the RMM memory pool.
        """
        ipchandle = (ctypes.c_byte * 64)()  # IPC handle is 64 bytes
        cuda.cudadrv.driver.driver.cuIpcGetMemHandle(
            ctypes.byref(ipchandle), memory.owner.handle,
        )
        source_info = cuda.current_context().device.get_device_identity()
        ptr = memory.device_ctypes_pointer.value
        offset = librmm.rmm_getallocationoffset(ptr, 0)
        return IpcHandle(
            memory, ipchandle, memory.size, source_info, offset=offset
        )

    def get_memory_info(self):
        return get_info()

    @property
    def interface_version(self):
        return 1


# Enables the use of RMM for Numba via an environment variable setting,
# NUMBA_CUDA_MEMORY_MANAGER=rmm. See:
# http://numba.pydata.org/numba-doc/latest/cuda/external-memory.html#environment-variable
_numba_memory_manager = RMMNumbaManager


try:
    import cupy
except Exception:
    cupy = None


def rmm_cupy_allocator(nbytes):
    """
    A CuPy allocator that make use of RMM.

    Examples
    --------
    >>> import rmm
    >>> import cupy
    >>> cupy.cuda.set_allocator(rmm.rmm_cupy_allocator)
    """
    if cupy is None:
        raise ModuleNotFoundError("No module named 'cupy'")

    buf = librmm.device_buffer.DeviceBuffer(size=nbytes)
    dev_id = -1 if buf.ptr else cupy.cuda.device.get_device_id()
    mem = cupy.cuda.UnownedMemory(
        ptr=buf.ptr, size=buf.size, owner=buf, device_id=dev_id
    )
    ptr = cupy.cuda.memory.MemoryPointer(mem, 0)

    return ptr


def _make_finalizer(handle, allocations):
    """
    Factory to make the finalizer function.
    We need to bind *handle* and *stream* into the actual finalizer, which
    takes no args.
    """

    def finalizer():
        """
        Invoked when the MemoryPointer is freed
        """
        # At exit time (particularly in the Numba test suite) allocations may
        # have already been cleaned up by a call to Context.reset() for the
        # context, even if there are some DeviceNDArrays and their underlying
        # allocations lying around. Finalizers then get called by weakref's
        # atexit finalizer, at which point allocations[handle] no longer
        # exists. This is harmless, except that a traceback is printed just
        # prior to exit (without abnormally terminating the program), but is
        # worrying for the user. To avoid the traceback, we check if
        # allocations is already empty.
        #
        # In the case where allocations is not empty, but handle is not in
        # allocations, then something has gone wrong - so we only guard against
        # allocations being completely empty, rather than handle not being in
        # allocations.
        if allocations:
            del allocations[handle]

    return finalizer


def _register_atexit_finalize():
    """
    Registers rmmFinalize() with ``std::atexit``.
    """
    librmm.register_atexit_finalize()
