"""Правка .env из бота: заменить или дописать ключи, не трогая остальное."""
import os
import re
from pathlib import Path

_PLAIN = re.compile(r'^[A-Za-z0-9@._+\-:/%!]*$')


def quote(value: str) -> str:
    """Значение для .env: простые пишем как есть, остальные — в одинарных кавычках.

    python-dotenv в одинарных кавычках раскрывает только \\\\ и \\', поэтому
    пароль с пробелами, «#» или кавычками прочитается ровно таким, как введён.
    """
    if _PLAIN.match(value):
        return value
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


def set_values(path: Path, values: dict[str, str], comment: str = '') -> None:
    """Записать ключи в .env. Существующие строки заменяются на месте, новые
    дописываются в конец (с комментарием). Права 600 и владелец сохраняются."""
    lines = path.read_text(encoding='utf-8').splitlines() if path.exists() else []
    left = dict(values)
    out = []
    for line in lines:
        key = line.split('=', 1)[0].strip()
        if '=' in line and not line.lstrip().startswith('#') and key in left:
            out.append(f'{key}={quote(left.pop(key))}')
        else:
            out.append(line)
    if left:
        if out and out[-1].strip():
            out.append('')
        if comment:
            out.append(f'# {comment}')
        out += [f'{k}={quote(v)}' for k, v in left.items()]

    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text('\n'.join(out) + '\n', encoding='utf-8')
    os.chmod(tmp, 0o600)  # там токены и пароли
    if path.exists() and hasattr(os, 'chown'):
        st = path.stat()
        try:
            os.chown(tmp, st.st_uid, st.st_gid)
        except PermissionError:
            pass
    tmp.replace(path)
