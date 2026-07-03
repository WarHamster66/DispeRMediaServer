#!/usr/bin/env python3
"""
DispeR Media Server — перенос медиапапок на новый диск.
Запуск от root:  sudo python3 migrate_disk.py

Что делает:
  1. Находит подключённые диски (включая новые/несмонтированные).
  2. Предлагает отформатировать выбранный диск (с двойным подтверждением).
  3. Монтирует его и прописывает в /etc/fstab (nofail — сервер загрузится и без диска).
  4. Переносит выбранные медиапапки (rsync) и делает bind-монтирования,
     чтобы ВСЕ пути остались прежними — боту, Plex, Samba и Transmission
     ничего перенастраивать не нужно.
  5. Ставит правильные права (группа media, setgid).
  6. Опционально создаёт дополнительную папку на системном диске.
"""

import json
import subprocess
import sys
from pathlib import Path

# ── ANSI цвета ─────────────────────────────────────────────────────────────────
R = '\033[91m'; G = '\033[92m'; Y = '\033[93m'; B = '\033[94m'
M = '\033[95m'; C = '\033[96m'; RS = '\033[0m'; BD = '\033[1m'

PROJECT_DIR = Path(__file__).parent.resolve()
CFG_PATH = PROJECT_DIR / 'config.json'
FSTAB = Path('/etc/fstab')
SERVICES = ['media-server', 'transmission-daemon', 'plexmediaserver']


def ok(msg): print(f"  {G}✓{RS}  {msg}")
def info(msg): print(f"  {C}→{RS}  {msg}")
def warn(msg): print(f"  {Y}⚠{RS}  {msg}")
def err(msg): print(f"  {R}✗{RS}  {msg}")


def ask(prompt, default=''):
    dflt = f'  [{default}]' if default else ''
    try:
        v = input(f"  {M}?{RS}  {prompt}{dflt}: ").strip()
        return v if v else default
    except (KeyboardInterrupt, EOFError):
        print("\nОтменено."); sys.exit(0)


def ask_bool(prompt, default=True):
    v = ask(f"{prompt} ({'Y/n' if default else 'y/N'})", '').lower()
    return default if not v else v.startswith('y')


def run(cmd, check=True, capture=False):
    if capture:
        return subprocess.run(cmd, capture_output=True, text=True)
    return subprocess.run(cmd, check=check)


def human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def list_devices():
    """Все блочные устройства (диски и разделы) кроме системного."""
    r = run(['lsblk', '-J', '-b', '-o', 'PATH,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL'], capture=True)
    data = json.loads(r.stdout)

    # Определяем системный диск (на котором корень)
    root_dev = run(['findmnt', '-n', '-o', 'SOURCE', '/'], capture=True).stdout.strip()

    devices = []
    def walk(nodes, parent_path=''):
        for n in nodes:
            path = n.get('path') or ''
            is_system = root_dev.startswith(path) or path.startswith(root_dev.rstrip('0123456789p'))
            devices.append({
                'path': path,
                'size': int(n.get('size') or 0),
                'type': n.get('type'),
                'fstype': n.get('fstype') or '',
                'mnt': n.get('mountpoint') or '',
                'model': (n.get('model') or '').strip(),
                'system': is_system,
            })
            walk(n.get('children', []), path)
    walk(data.get('blockdevices', []))
    return [d for d in devices if d['type'] in ('disk', 'part')]


def append_fstab(line: str) -> None:
    text = FSTAB.read_text()
    if line.split()[0] in text and line.split()[1] in text:
        return  # уже есть
    FSTAB.write_text(text.rstrip() + '\n' + line + '\n')


def main():
    import os
    if os.geteuid() != 0:
        print(f"\n{R}Запусти от root:{RS}  sudo python3 migrate_disk.py\n")
        sys.exit(1)

    print(f"""
{B}╔══════════════════════════════════════════════════════════╗
║      DispeR Media Server — перенос на новый диск         ║
╚══════════════════════════════════════════════════════════╝{RS}""")

    cfg = json.loads(CFG_PATH.read_text(encoding='utf-8'))
    shared = cfg['SHARED_FOLDER'].rstrip('/')
    folders = cfg.get('ALLOWED_FOLDERS', [])

    # ── 1. Выбор диска ──
    devices = list_devices()
    print(f"\n{BD}Подключённые устройства:{RS}\n")
    candidates = []
    for d in devices:
        mark = f"{R}(системный — нельзя){RS}" if d['system'] else ''
        mnt = f"→ {d['mnt']}" if d['mnt'] else '(не смонтирован)'
        fs = d['fstype'] or 'нет ФС'
        line = f"  {d['path']:<16} {human(d['size']):>10}  {d['type']:<5} {fs:<8} {mnt} {d['model']} {mark}"
        if d['system']:
            print(line)
        else:
            candidates.append(d)
            print(f"  {BD}[{len(candidates)}]{RS}{line}")

    if not candidates:
        err("Не найдено ни одного диска кроме системного. Подключи диск и запусти снова.")
        sys.exit(1)

    n = ask("Номер диска для медиатеки")
    if not n.isdigit() or not 1 <= int(n) <= len(candidates):
        err("Неверный номер."); sys.exit(1)
    dev = candidates[int(n) - 1]

    # ── 2. Форматирование ──
    if dev['mnt'] in ('/', '/boot', '/boot/efi'):
        err("Это системный раздел."); sys.exit(1)

    do_format = False
    if not dev['fstype']:
        do_format = ask_bool(f"На {dev['path']} нет файловой системы. Отформатировать в ext4?", True)
        if not do_format:
            err("Без файловой системы продолжить нельзя."); sys.exit(1)
    else:
        warn(f"На {dev['path']} уже есть ФС ({dev['fstype']}).")
        do_format = ask_bool("Отформатировать заново? (СОТРЁТ ВСЕ ДАННЫЕ НА НЁМ)", False)

    if do_format:
        confirm = ask(f"{R}Для подтверждения введи путь устройства ({dev['path']}){RS}")
        if confirm != dev['path']:
            err("Подтверждение не совпало — отмена."); sys.exit(1)
        if dev['mnt']:
            run(['umount', dev['path']], check=False)
        info(f"Форматирую {dev['path']} в ext4…")
        run(['mkfs.ext4', '-F', '-L', 'media', dev['path']])
        ok("Отформатирован")

    # ── 3. Монтирование ──
    if dev['mnt'] and not do_format:
        mnt = dev['mnt']
        ok(f"Диск уже смонтирован: {mnt}")
    else:
        mnt = ask("Точка монтирования", '/media/storage')
        Path(mnt).mkdir(parents=True, exist_ok=True)
        uuid = run(['blkid', '-s', 'UUID', '-o', 'value', dev['path']], capture=True).stdout.strip()
        fstype = run(['blkid', '-s', 'TYPE', '-o', 'value', dev['path']], capture=True).stdout.strip() or 'ext4'
        append_fstab(f"UUID={uuid} {mnt} {fstype} defaults,nofail 0 2")
        run(['systemctl', 'daemon-reload'], check=False)
        run(['mount', mnt])
        ok(f"Смонтирован: {mnt} (и прописан в fstab, nofail)")

    # ── 4. Выбор папок для переноса ──
    print(f"\n{BD}Какие папки перенести на {mnt}?{RS}")
    print("  (Torrent обычно оставляют на системном диске — качается быстрее,\n   а готовое кино переносится в медиапапки)\n")
    to_move = []
    for f in folders:
        default = (f != 'Torrent')
        if ask_bool(f"Перенести «{f}»?", default):
            to_move.append(f)

    if to_move:
        info("Останавливаю сервисы на время переноса…")
        for s in SERVICES:
            run(['systemctl', 'stop', s], check=False)

        try:
            for f in to_move:
                src = f'{shared}/{f}'
                dst = f'{mnt}/{f}'
                Path(dst).mkdir(parents=True, exist_ok=True)
                if Path(src).is_dir() and not Path(src).is_mount():
                    size_out = run(['du', '-sh', src], capture=True).stdout.split()
                    total = size_out[0] if size_out else '?'
                    info(f"Переношу «{f}» ({total})… прогресс ниже:")
                    # --info=progress2 — общий прогресс: скопировано / % / скорость
                    # --no-inc-recursive — сначала считает объём, чтобы % был честным
                    r = run(['rsync', '-a', '--info=progress2', '--no-inc-recursive',
                             f'{src}/', f'{dst}/'], check=False)
                    print()
                    if r.returncode != 0:
                        err(f"rsync для «{f}» завершился с ошибкой — папка НЕ удалена, продолжаю со следующей.")
                        continue
                    run(['rm', '-rf', src])
                Path(src).mkdir(parents=True, exist_ok=True)
                append_fstab(f"{dst} {src} none bind,nofail,x-systemd.requires-mounts-for={mnt} 0 0")
                ok(f"«{f}» теперь на {mnt} (путь {src} не изменился)")

            run(['systemctl', 'daemon-reload'], check=False)
            run(['mount', '-a'], check=False)

            # Права: та же схема, что и у установщика
            run(['chown', '-R', 'root:media', mnt], check=False)
            run(['chmod', '-R', '2775', mnt], check=False)
            ok("Права выставлены (группа media)")
        finally:
            for s in SERVICES:
                run(['systemctl', 'start', s], check=False)

    # ── 5. Дополнительная папка на системном диске ──
    print()
    extra = ask("Создать доп. папку на системном диске (имя; Enter — пропустить)", '')
    if extra:
        p = Path(shared) / extra
        p.mkdir(parents=True, exist_ok=True)
        run(['chown', 'root:media', str(p)], check=False)
        run(['chmod', '2775', str(p)], check=False)
        if extra not in cfg['ALLOWED_FOLDERS']:
            cfg['ALLOWED_FOLDERS'].append(extra)
        if extra not in cfg.get('PROTECTED_FOLDERS', []):
            cfg.setdefault('PROTECTED_FOLDERS', []).append(extra)
        ok(f"Папка «{extra}» создана и добавлена в медиатеку (останется на системном диске)")

    # ── 6. Мониторинг места ──
    if ask_bool(f"Следить за местом на новом диске в отчётах/алертах (DISK_PATH={mnt})?", True):
        cfg['DISK_PATH'] = mnt

    CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=4), encoding='utf-8')
    run(['systemctl', 'restart', 'media-server'], check=False)

    print(f"""
{G}{'═' * 60}{RS}
{BD}{G}  ✅  Перенос завершён!{RS}
{G}{'═' * 60}{RS}
  Новый диск:      {mnt}
  Перенесены:      {', '.join(to_move) if to_move else '—'}
  Пути НЕ изменились — бот, Plex и Samba работают как раньше.
  Проверь в боте:  /disks  и  /report
""")


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as e:
        err(f"Команда завершилась с ошибкой: {e.cmd}")
        sys.exit(1)
