import asyncio
from weakref import WeakValueDictionary

_user_locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()


def get_user_lock(user_id: int) -> asyncio.Lock:
    lock = _user_locks.get(user_id)
    if lock is not None:
        return lock
    lock = asyncio.Lock()
    _user_locks[user_id] = lock
    # Holders and waiters keep their lock alive; unused locks can be collected.
    return lock
