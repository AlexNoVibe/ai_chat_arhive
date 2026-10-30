#!/usr/bin/env python3
"""
find_duplicate_archives.py — Check and find chat export archives that duplicate records.

Identifies archives whose records (chats) are 100% contained within another archive,
outputs full paths, and suggests safe deletion.
"""

import os
import sys
import json
import argparse
import zipfile
import shutil
import time
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple, Set

# Add parent directory to sys.path so ai_chat_archive can be imported cleanly
ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Ensure stdout and stderr handle Unicode properly on Windows consoles (cp1251/cp866)
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

try:
    from ai_chat_archive import (
        FormatRegistry,
        load_json_lenient,
        repair_json_text,
        Chat,
        Message,
    )
except ImportError as err:
    print(f"Error: failed to import ai_chat_archive: {err}", file=sys.stderr)
    sys.exit(1)


def print_progress(current: int, total: int, prefix: str = "", item: str = ""):
    """Print an updating progress line in terminal."""
    if total <= 0:
        return
    pct = int(current * 100 / total)
    name = Path(item).name if item else ""
    if len(name) > 30:
        name = name[:27] + "..."
    if sys.stdout.isatty():
        msg = f"\r{prefix} [{current}/{total}] {pct}% {name}"
        sys.stdout.write(msg.ljust(79)[:79])
        sys.stdout.flush()
    else:
        if current == total or current == 1 or (current % max(1, total // 5) == 0):
            print(f"{prefix} [{current}/{total}] {pct}% {name}", flush=True)


def clear_progress():
    """Clear progress line in terminal."""
    if sys.stdout.isatty():
        sys.stdout.write("\r" + " " * 79 + "\r")
        sys.stdout.flush()


@dataclass
class ArchiveInfo:
    path: str
    abs_path: str
    size_bytes: int
    mtime: float
    chats: List[Chat]
    _id_index: Dict[str, Chat] = field(default_factory=dict, repr=False)
    _first_msg_index: Dict[Tuple[str, str], List[Chat]] = field(default_factory=dict, repr=False)

    @property
    def chat_count(self) -> int:
        return len(self.chats)

    def build_indexes(self):
        self._id_index = {}
        self._first_msg_index = {}
        for c in self.chats:
            cid = (c.chat_id or "").strip()
            if cid and not cid.startswith("http"):
                self._id_index[cid] = c
            if c.messages:
                m0 = c.messages[0]
                key = (m0.role, normalize_content(m0.content))
                self._first_msg_index.setdefault(key, []).append(c)


def normalize_content(text: str) -> str:
    """Normalize whitespace in text for robust comparisons."""
    return " ".join((text or "").split())


def is_chat_covered(chat_a: Chat, chat_b: Chat) -> bool:
    """
    Strictly verify if chat_a is 100% preserved inside chat_b.
    Zero risk rules:
      1. If non-generic IDs (e.g. UUIDs) are present on both, they MUST be identical.
      2. If chat_a has messages, chat_b MUST contain all messages of chat_a
         in the exact same order with identical roles and normalized contents.
      3. If chat_a has no messages, titles or timestamps must match.
    """
    cid_a = (chat_a.chat_id or "").strip()
    cid_b = (chat_b.chat_id or "").strip()

    # If both have explicit non-URL IDs, they must match
    if cid_a and cid_b and not cid_a.startswith("http") and not cid_b.startswith("http"):
        if cid_a != cid_b:
            return False

    msgs_a = [(m.role, normalize_content(m.content)) for m in chat_a.messages]
    msgs_b = [(m.role, normalize_content(m.content)) for m in chat_b.messages]

    # Content verification: every single message in chat_a must exist in chat_b
    if msgs_a:
        if len(msgs_b) < len(msgs_a):
            return False
        # Exact prefix or full equality check
        if msgs_b[:len(msgs_a)] != msgs_a:
            return False
        return True

    # Empty chat edge cases
    if chat_a.title and chat_b.title and chat_a.title == chat_b.title:
        return True

    if chat_a.created_at and chat_b.created_at and abs(chat_a.created_at - chat_b.created_at) < 1.0:
        return True

    return False


def find_covering_chat(ca: Chat, arch_b: ArchiveInfo) -> Optional[Chat]:
    """Find a chat in arch_b that 100% covers ca, using indexes for fast lookup."""
    cid = (ca.chat_id or "").strip()
    # 1. Lookup by non-generic ID
    if cid and not cid.startswith("http") and cid in arch_b._id_index:
        candidate = arch_b._id_index[cid]
        if is_chat_covered(ca, candidate):
            return candidate

    # 2. Lookup by first message (exact match)
    if ca.messages:
        m0 = ca.messages[0]
        key = (m0.role, normalize_content(m0.content))
        for candidate in arch_b._first_msg_index.get(key, []):
            if is_chat_covered(ca, candidate):
                return candidate
        return None

    # 3. Fallback for empty chats: check matching title
    for candidate in arch_b.chats:
        if is_chat_covered(ca, candidate):
            return candidate
    return None


def load_archive(file_path: str) -> Optional[ArchiveInfo]:
    """Parse JSON or ZIP export file into an ArchiveInfo object."""
    p = Path(file_path).resolve()
    if not p.is_file():
        return None

    size_bytes = p.stat().st_size
    mtime = p.stat().st_mtime
    chats: List[Chat] = []

    ext = p.suffix.lower()
    if ext == ".json":
        try:
            data, _ = load_json_lenient(str(p))
            chats = FormatRegistry.parse(data, str(p), on_unknown="try")
        except Exception:
            return None
    elif ext == ".zip":
        try:
            with zipfile.ZipFile(str(p), "r") as z:
                for name in z.namelist():
                    if name.lower().endswith(".json") and not name.startswith("__MACOSX"):
                        try:
                            raw = z.read(name).decode("utf-8", errors="replace")
                            data = json.loads(repair_json_text(raw))
                            sub = FormatRegistry.parse(data, f"{p}!{name}", on_unknown="try")
                            chats.extend(sub)
                        except Exception:
                            pass
        except Exception:
            return None
    else:
        return None

    info = ArchiveInfo(
        path=file_path,
        abs_path=str(p),
        size_bytes=size_bytes,
        mtime=mtime,
        chats=chats,
    )
    info.build_indexes()
    return info


def collect_archives(root_dir: str, show_progress: bool = True) -> List[ArchiveInfo]:
    """Recursively discover and parse all JSON/ZIP chat archives under root_dir."""
    archives = []
    root = Path(root_dir).resolve()
    if not root.exists():
        return []

    raw_paths = []
    for dirpath, _, filenames in os.walk(root):
        for fn in sorted(filenames):
            ext = os.path.splitext(fn)[1].lower()
            if ext in (".json", ".zip"):
                raw_paths.append(os.path.join(dirpath, fn))

    total = len(raw_paths)
    for idx, full_path in enumerate(raw_paths, 1):
        if show_progress:
            print_progress(idx, total, prefix="⏳ Загрузка файлов:", item=full_path)
        info = load_archive(full_path)
        if info and info.chat_count > 0:
            archives.append(info)

    if show_progress:
        clear_progress()
    return archives


@dataclass
class DuplicateMatch:
    archive: ArchiveInfo
    keeper: ArchiveInfo
    matched_count: int
    total_count: int
    is_exact_copy: bool


@dataclass
class PartialMatch:
    archive: ArchiveInfo
    other: ArchiveInfo
    matched_count: int
    total_count: int


def analyze_duplicates(
    archives: List[ArchiveInfo],
    show_progress: bool = True,
) -> Tuple[List[DuplicateMatch], List[PartialMatch]]:
    """
    Analyze pairwise coverage between archives.
    Returns:
      - full_duplicates: archives that are 100% duplicated by another archive and can be deleted.
      - partial_duplicates: archives that share records but have unique records (do not delete).
    """
    n = len(archives)
    # coverage_matrix[i][j] = number of chats in archives[i] covered by archives[j]
    coverage_matrix = [[0] * n for _ in range(n)]

    for i in range(n):
        arch_a = archives[i]
        if show_progress:
            print_progress(i + 1, n, prefix="🔍 Сравнение архивов:", item=arch_a.abs_path)
        chats_a = arch_a.chats
        for j in range(n):
            if i == j:
                coverage_matrix[i][j] = len(chats_a)
                continue
            arch_b = archives[j]
            cov = sum(1 for ca in chats_a if find_covering_chat(ca, arch_b) is not None)
            coverage_matrix[i][j] = cov

    if show_progress:
        clear_progress()

    # Identify identical pairs and subsets
    deleted_indices: Set[int] = set()
    full_duplicates: List[DuplicateMatch] = []
    partial_matches: List[PartialMatch] = []

    # First pass: check for supersets (B has strictly more chats or is chosen over A)
    # Sort files by chat_count descending so larger archives act as keepers
    indexed_by_size = sorted(range(n), key=lambda idx: (archives[idx].chat_count, archives[idx].mtime), reverse=True)

    for i in range(n):
        if i in deleted_indices:
            continue
        total_a = archives[i].chat_count
        best_keeper_idx: Optional[int] = None
        best_keeper_score = -1
        is_exact = False

        for j in range(n):
            if i == j or j in deleted_indices:
                continue

            cov = coverage_matrix[i][j]
            if cov == total_a:
                # archives[j] fully covers archives[i]
                total_b = archives[j].chat_count
                cov_b_by_a = coverage_matrix[j][i]
                mutual = (cov_b_by_a == total_b and total_a == total_b)

                if mutual:
                    # Both files have identical records. Break tie:
                    # Keep j if j has newer mtime, or shorter path, or alphabetical tie-breaker
                    if (archives[j].mtime, -len(archives[j].abs_path), archives[j].abs_path) > \
                       (archives[i].mtime, -len(archives[i].abs_path), archives[i].abs_path):
                        best_keeper_idx = j
                        is_exact = True
                        break
                else:
                    # j has more records, or covers i without i covering j
                    if total_b > best_keeper_score:
                        best_keeper_score = total_b
                        best_keeper_idx = j
                        is_exact = False

        if best_keeper_idx is not None:
            deleted_indices.add(i)
            full_duplicates.append(
                DuplicateMatch(
                    archive=archives[i],
                    keeper=archives[best_keeper_idx],
                    matched_count=total_a,
                    total_count=total_a,
                    is_exact_copy=is_exact,
                )
            )

    # Second pass: detect partial overlaps for archives not marked as full duplicates
    for i in range(n):
        if i in deleted_indices:
            continue
        total_a = archives[i].chat_count
        for j in range(n):
            if i == j or j in deleted_indices:
                continue
            cov = coverage_matrix[i][j]
            if 0 < cov < total_a:
                partial_matches.append(
                    PartialMatch(
                        archive=archives[i],
                        other=archives[j],
                        matched_count=cov,
                        total_count=total_a,
                    )
                )

    return full_duplicates, partial_matches


def print_report(
    root_dir: str,
    archives: List[ArchiveInfo],
    full_duplicates: List[DuplicateMatch],
    partial_matches: List[PartialMatch],
    verbose: bool = False,
):
    print("=" * 80)
    print(f"🔍 ПОИСК ДУБЛИКАТОВ АРХИВОВ ЧАТОВ")
    print(f"   Папка: {os.path.abspath(root_dir)}")
    print(f"   Найдено архивов: {len(archives)}")
    total_chats = sum(a.chat_count for a in archives)
    print(f"   Всего записей (диалогов): {total_chats}")
    print("=" * 80)

    if not full_duplicates:
        print("\n✅ Полных дубликатов архивов не найдено. Все архивы содержат уникальные записи.")
    else:
        print(f"\n⚠️  НАЙДЕНО ПОЛНЫХ ДУБЛИКАТОВ: {len(full_duplicates)}")
        print("   Все записи этих файлов уже присутствуют в других архивах.")
        print("   Их можно безопасно удалить:\n")

        for idx, match in enumerate(full_duplicates, 1):
            arch = match.archive
            keeper = match.keeper
            rel_type = "Полная идентичная копия" if match.is_exact_copy else "Полное подмножество"

            print(f"[{idx}] МОЖЕТЕ УДАЛИТЬ ЭТО:")
            print(f"    Путь:    {arch.abs_path}")
            print(f"    Записей: {arch.chat_count} шт.")
            print(f"    Причина: {rel_type}.")
            print(f"             Все {match.matched_count} из {arch.chat_count} записей содержатся в:")
            print(f"             -> {keeper.abs_path} ({keeper.chat_count} шт.)")
            print()

    if partial_matches and verbose:
        print("-" * 80)
        print("ℹ️  ЧАСТИЧНЫЕ ДУБЛИКАТЫ (НЕ УДАЛЯТЬ — СОДЕРЖАТ УНИКАЛЬНЫЕ ЗАПИСИ):")
        seen_partial = set()
        for p in partial_matches:
            key = (p.archive.abs_path, p.other.abs_path)
            if key in seen_partial:
                continue
            seen_partial.add(key)
            missing = p.total_count - p.matched_count
            print(f"  • {p.archive.abs_path}")
            print(f"    {p.matched_count} из {p.total_count} записей есть в {p.other.abs_path}")
            print(f"    ⚠️ {missing} уникальных записей будут утеряны при удалении! Не удаляйте этот файл.")
            print()

    print("=" * 80)
    print("ИТОГ:")
    print(f"  Всего проверено файлов: {len(archives)}")
    print(f"  Файлов к удалению:      {len(full_duplicates)}")

    if full_duplicates:
        print("\nСписок путей для копирования или удаления:")
        for match in full_duplicates:
            print(f'"{match.archive.abs_path}"')
    print("=" * 80)


def handle_deletion(
    full_duplicates: List[DuplicateMatch],
    target_dir: str,
    permanent: bool = False,
    interactive: bool = False,
    auto_confirm: bool = False,
):
    """
    Safely handle deletion of duplicate archives with mandatory user confirmation.
    By default, moves files to a backup directory (_duplicates_backup).
    """
    if not full_duplicates:
        return

    backup_dir = Path(target_dir).resolve() / "_duplicates_backup"

    print("\n" + "=" * 80)
    print("⚠️  ЗАПРОС НА УДАЛЕНИЕ ДУБЛИКАТОВ")
    print(f"   Кандидатов на удаление: {len(full_duplicates)} файл(ов)")
    if permanent:
        print("   Режим: БЕЗВОЗВРАТНОЕ УДАЛЕНИЕ С ДИСКА (--permanent)")
    else:
        print(f"   Режим: БЕЗОПАСНОЕ ПЕРЕМЕЩЕНИЕ В РЕЗЕРВНУЮ ПАПКУ:")
        print(f"          {backup_dir}")
    print("=" * 80)

    for idx, match in enumerate(full_duplicates, 1):
        arch = match.archive
        size_kb = max(1, arch.size_bytes // 1024)
        print(f"  [{idx}] {arch.abs_path}")
        print(f"      Размер: {size_kb} КБ | Диалогов: {arch.chat_count}")
        print(f"      Все диалоги полностью сохранены в:")
        print(f"      -> {match.keeper.abs_path} ({match.keeper.chat_count} диалогов)")
        # Show first 3 chat titles as proof
        titles = [c.title for c in arch.chats[:3]]
        titles_str = ", ".join(f'"{t}"' for t in titles)
        if len(arch.chats) > 3:
            titles_str += f" и еще {len(arch.chats) - 3}..."
        print(f"      Примеры диалогов: {titles_str}")
        print()

    # Mandatory user confirmation
    if not auto_confirm:
        print("=" * 80)
        print("🔒 ПОДТВЕРЖДЕНИЕ СУПЕР ОБЯЗАТЕЛЬНО!")
        if permanent:
            print("   Файлы будут УНИЧТОЖЕНЫ БЕЗВОЗВРАТНО.")
            prompt = "   Введите точное слово 'УДАЛИТЬ' или 'DELETE' для продолжения: "
            valid_responses = {"УДАЛИТЬ", "DELETE"}
        else:
            print("   Файлы будут ПЕРЕМЕЩЕНЫ в папку резервной копии (вы сможете их восстановить).")
            prompt = "   Введите 'УДАЛИТЬ', 'DELETE', 'ДА' или 'YES' для продолжения: "
            valid_responses = {"УДАЛИТЬ", "DELETE", "ДА", "YES"}

        try:
            resp = input(prompt).strip().upper()
            if resp not in valid_responses:
                print("\n❌ Подтверждение не введено. Операция отменена, файлы не тронуты.")
                return
        except EOFError:
            print("\n❌ Неинтерактивный ввод: подтверждение не получено. Операция отменена.")
            return

    if not permanent:
        backup_dir.mkdir(parents=True, exist_ok=True)

    print("\nВыполнение операции:")
    for idx, match in enumerate(full_duplicates, 1):
        src_path = match.archive.abs_path
        if not os.path.exists(src_path):
            continue

        if interactive and not auto_confirm:
            try:
                ask = input(f"  [{idx}/{len(full_duplicates)}] Обработать {Path(src_path).name}? [y/N/q]: ").strip().lower()
                if ask in ("q", "й"):
                    print("Прервано пользователем.")
                    break
                if ask not in ("y", "yes", "д", "да"):
                    print("  Пропущен.")
                    continue
            except EOFError:
                break

        try:
            if permanent:
                os.remove(src_path)
                print(f"  🗑️ Безвозвратно удален: {src_path}")
            else:
                dest_file = backup_dir / Path(src_path).name
                if dest_file.exists():
                    timestamp = time.strftime("%Y%m%d_%H%M%S")
                    dest_file = backup_dir / f"{dest_file.stem}_{timestamp}{dest_file.suffix}"
                shutil.move(src_path, str(dest_file))
                print(f"  📦 Перемещен в бэкап: {dest_file.name}")
        except Exception as e:
            print(f"  ❌ Ошибка обработки {src_path}: {e}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Поиск архивов чатов, которые полностью дублируют записи других архивов."
    )
    parser.add_argument(
        "directory",
        nargs="?",
        default="",
        help="Папка с экспортами чатов (по умолчанию .\\chat_export или текущая папка)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Подробный вывод (включая частичные дубликаты)",
    )
    parser.add_argument(
        "--delete",
        action="store_true",
        help="Удалить или переместить в бэкап найденные полные дубликаты после обязательного подтверждения",
    )
    parser.add_argument(
        "--permanent",
        action="store_true",
        help="Безвозвратное удаление вместо безопасного перемещения в _duplicates_backup",
    )
    parser.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Пофайловый интерактивный запрос перед удалением каждого файла",
    )
    parser.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="Автоматическое подтверждение (для автоматических скриптов и тестов)",
    )

    args = parser.parse_args()

    # Determine directory to scan
    target_dir = args.directory
    if not target_dir:
        default_dir = os.path.join(os.getcwd(), "chat_export")
        target_dir = default_dir if os.path.isdir(default_dir) else os.getcwd()

    if not os.path.isdir(target_dir):
        print(f"Ошибка: папка не найдена: {target_dir}", file=sys.stderr)
        sys.exit(1)

    archives = collect_archives(target_dir)
    if not archives:
        print(f"В папке {target_dir} не найдено поддерживаемых архивов с чатами (.json, .zip).")
        sys.exit(0)

    full_duplicates, partial_matches = analyze_duplicates(archives)
    print_report(target_dir, archives, full_duplicates, partial_matches, verbose=args.verbose)

    if args.delete and full_duplicates:
        handle_deletion(
            full_duplicates=full_duplicates,
            target_dir=target_dir,
            permanent=args.permanent,
            interactive=args.interactive,
            auto_confirm=args.yes,
        )


if __name__ == "__main__":
    main()
