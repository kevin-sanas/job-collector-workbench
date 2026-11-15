import uuid
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def atomic_destination(path: Path):
    target = Path(path)
    temporary = target.with_name('.' + target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        yield temporary
        temporary.replace(target)
    finally:
        temporary.unlink(missing_ok=True)

