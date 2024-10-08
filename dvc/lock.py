"""Manages dvc lock file."""

import hashlib
import os
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import Optional, Union

import flufl.lock
import zc.lockfile
from funcy import retry

from dvc.exceptions import DvcException
from dvc.progress import Tqdm
from dvc.utils import format_link

DEFAULT_TIMEOUT = 3


FAILED_TO_LOCK_MESSAGE = (
    "Unable to acquire lock. Most likely another DVC process is running or "
    "was terminated abruptly. Check the page {} for other possible reasons "
    "and to learn how to resolve this."
).format(format_link("https://dvc.org/doc/user-guide/troubleshooting#lock-issue"))


class LockError(DvcException):
    """Thrown when unable to acquire the lock for DVC repo."""


class LockBase(ABC):
    @abstractmethod
    def __init__(self, lockfile):
        self._lockfile = lockfile

    @property
    def lockfile(self):
        return self._lockfile

    @abstractmethod
    def lock(self):
        pass

    @abstractmethod
    def unlock(self):
        pass

    @property
    @abstractmethod
    def is_locked(self):
        pass

    @abstractmethod
    def __enter__(self):
        pass

    @abstractmethod
    def __exit__(self, typ, value, tbck):
        pass


class LockNoop(LockBase):
    def __init__(self, *args, **kwargs):
        self._lock = False

    def lock(self):
        self._lock = True

    def unlock(self):
        if not self.is_locked:
            raise DvcException("Unlock called on an unlocked lock")
        self._lock = False

    @property
    def is_locked(self):
        return self._lock

    def __enter__(self):
        self.lock()

    def __exit__(self, typ, value, tbck):
        self.unlock()


class Lock(LockBase):
    """Class for DVC repo lock.

    Uses zc.lockfile as backend.
    """

    def __init__(self, lockfile, friendly=False, **kwargs):
        super().__init__(lockfile)
        self._friendly = friendly
        self._lock = None
        self._lock_failed = False

    @property
    def files(self):
        return [self._lockfile]

    def _do_lock(self):
        try:
            self._lock_failed = False
            with Tqdm(
                bar_format="{desc}",
                disable=not self._friendly,
                desc="If DVC froze, see `hardlink_lock` in {}".format(
                    format_link("https://man.dvc.org/config#core")
                ),
            ):
                self._lock = zc.lockfile.LockFile(self._lockfile)
        except zc.lockfile.LockError:
            self._lock_failed = True
            raise LockError(FAILED_TO_LOCK_MESSAGE)  # noqa: B904

    def lock(self):
        retries = 6
        delay = DEFAULT_TIMEOUT / retries
        lock_retry = retry(retries, LockError, timeout=delay)(self._do_lock)
        lock_retry()

    def unlock(self):
        if self._lock_failed:
            assert self._lock is None
            return

        if not self.is_locked:
            raise DvcException("Unlock called on an unlocked lock")
        assert self._lock
        self._lock.close()
        self._lock = None

    @property
    def is_locked(self):
        return bool(self._lock)

    def __enter__(self):
        self.lock()

    def __exit__(self, typ, value, tbck):
        self.unlock()


class HardlinkLock(flufl.lock.Lock, LockBase):
    """Class for DVC repo lock.

    Args:
        lockfile (str): the lock filename
            in.
        tmp_dir (str): a directory to store claim files.
    """

    def __init__(self, lockfile, tmp_dir=None, **kwargs):
        import socket

        self._tmp_dir = tmp_dir
        super().__init__(lockfile)

        # NOTE: this is basically Lock.__init__ copy-paste, except that
        # instead of using `socket.getfqdn()` we use `socket.gethostname()`
        # to speed this up. We've seen [1] `getfqdn()` take ~5sec to return
        # anything, which is way too slow. `gethostname()` is actually a
        # fallback for `getfqdn()` when it is not able to resolve a
        # canonical hostname through network. The claimfile that uses
        # `self._hostname` is still usable, as it uses `pid` and random
        # number to generate the resulting lock file name, which is unique
        # enough for our application.
        #
        # [1] https://github.com/iterative/dvc/issues/2582
        self._hostname = socket.gethostname()

        self._lifetime = timedelta(days=365)  # Lock for good by default
        self._separator = flufl.lock.SEP
        self._set_claimfile()
        self._owned = True
        self._retry_errnos = []

    def lock(self, timeout: Optional[Union[timedelta, int]] = None):
        try:
            super().lock(timeout or timedelta(seconds=DEFAULT_TIMEOUT))
        except flufl.lock.TimeOutError:
            raise LockError(FAILED_TO_LOCK_MESSAGE)  # noqa: B904

    def _set_claimfile(self):
        super()._set_claimfile()

        if self._tmp_dir is not None:
            # Under Windows file path length is limited so we hash it
            hasher = hashlib.md5(self._claimfile.encode())  # noqa: S324
            filename = hasher.hexdigest()
            self._claimfile = os.path.join(self._tmp_dir, filename + ".lock")


def make_lock(lockfile, tmp_dir=None, friendly=False, hardlink_lock=False):
    cls = HardlinkLock if hardlink_lock else Lock
    return cls(lockfile, tmp_dir=tmp_dir, friendly=friendly)
