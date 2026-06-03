#!/usr/bin/env python3
"""
Консолидация посуточных записей в помесячные в Google Sheets (Ресурс).

Использование:
    python consolidate.py --cutoff-date 2025-03-01
    python consolidate.py --keep-months 3
    python consolidate.py --keep-months 3 --dry-run

Параметры:
    --cutoff-date YYYY-MM-DD  Консолидировать все месяцы строго до указанного
    --keep-months N           Оставить последние N месяцев нетронутыми
    --dry-run                 Только показать что будет сделано, без изменений
    --yes                     Не запрашивать подтверждение
"""
import argparse
import sys
from datetime import date


def _compute_cutoff(keep: int, today: date) -> date:
    total = today.year * 12 + (today.month - 1)
    cutoff_total = max(12, total - keep + 1)
    return date(cutoff_total // 12, cutoff_total % 12 + 1, 1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Консолидация посуточных записей в помесячные в Google Sheets."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--cutoff-date", metavar="YYYY-MM-DD",
        help="Консолидировать всё до начала указанного месяца"
    )
    group.add_argument(
        "--keep-months", type=int, metavar="N",
        help="Оставить последние N месяцев без консолидации"
    )
    parser.add_argument("--dry-run", action="store_true", help="Только просмотр")
    parser.add_argument("--remove-empty-rows", action="store_true", help="Удалять пустые строки")
    parser.add_argument("--yes", "-y", action="store_true", help="Без подтверждения")
    args = parser.parse_args()

    from app import create_app
    app = create_app()

    with app.app_context():
        from flask import current_app
        today = date.today()

        if args.cutoff_date:
            try:
                raw = date.fromisoformat(args.cutoff_date)
                cutoff = date(raw.year, raw.month, 1)
            except ValueError:
                print(f"Ошибка: неверный формат даты '{args.cutoff_date}' (YYYY-MM-DD)")
                sys.exit(1)
        else:
            if args.keep_months < 0:
                print("Ошибка: --keep-months должен быть >= 0")
                sys.exit(1)
            cutoff = _compute_cutoff(args.keep_months, today)

        spreadsheet_id = current_app.config.get("GOOGLE_SHEETS_RESOURCE_ID", "")
        if not spreadsheet_id:
            print("Ошибка: GOOGLE_SHEETS_RESOURCE_ID не настроен в .env")
            sys.exit(1)

        sheet_name = current_app.config.get("GOOGLE_SHEETS_RESOURCE_SHEET", "resource")

        print(f"Spreadsheet:  {spreadsheet_id}")
        print(f"Лист:         {sheet_name}")
        print(f"Порог:        {cutoff.isoformat()} (месяцы до него будут консолидированы)")

        from services.sheets import get_sheets_service
        from services.consolidation import RESOURCE_SCHEMA, analyze_resource, consolidate_resource
        svc = get_sheets_service()

        remove_empty = args.remove_empty_rows

        if args.dry_run:
            print("\n--- Dry run (изменений не будет) ---")
            for line in analyze_resource(svc, spreadsheet_id, sheet_name, cutoff, RESOURCE_SCHEMA, remove_empty_rows=remove_empty):
                print(line)
            return

        if not args.yes:
            answer = input("\nПродолжить консолидацию? [y/N]: ").strip().lower()
            if answer != "y":
                print("Отменено.")
                return

        result = consolidate_resource(svc, spreadsheet_id, sheet_name, cutoff, RESOURCE_SCHEMA, remove_empty_rows=remove_empty)
        print(f"\nГотово:")
        print(f"  Консолидировано групп (счётчик/месяц): {result['consolidated']}")
        print(f"  Удалено посуточных строк:               {result['deleted']}")
        print(f"  Пропущено (уже помесячных):             {result['skipped']}")
        if result.get("empty_removed"):
            print(f"  Удалено пустых строк:                   {result['empty_removed']}")


if __name__ == "__main__":
    main()
