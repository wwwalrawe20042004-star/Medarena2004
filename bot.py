import argparse
from contextlib import closing
import hashlib
import html
import json
import logging
import os
from pathlib import Path
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


STAGES = (1, 2, 3, 4, 5)
STAGE_NAMES = ("الأولى", "الثانية", "الثالثة", "الرابعة", "الخامسة")
DEFAULT_DB = Path(__file__).with_name("codes.sqlite3")
STAGE_MENU = {
    "inline_keyboard": [
        [{"text": f"المرحلة {name}", "callback_data": f"stage:{stage}"}]
        for stage, name in zip(STAGES, STAGE_NAMES)
    ]
}
START_MENU = {"inline_keyboard": [[{"text": "ابدأ", "callback_data": "start"}]]}


def fingerprint(code):
    return hashlib.sha256(code.encode("utf-8")).digest()


def open_database(path):
    db = sqlite3.connect(path, timeout=10, isolation_level="IMMEDIATE")
    db.execute("PRAGMA secure_delete = ON")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS codes (
            id INTEGER PRIMARY KEY,
            fingerprint BLOB UNIQUE NOT NULL,
            stage INTEGER NOT NULL CHECK (stage BETWEEN 1 AND 5),
            code TEXT
        );
        CREATE INDEX IF NOT EXISTS available_codes
            ON codes (stage, id) WHERE code IS NOT NULL;
        CREATE TABLE IF NOT EXISTS claims (
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            PRIMARY KEY (chat_id, message_id)
        );
        """
    )
    return db


def import_codes(db, stage, path):
    added = skipped = 0
    with db, path.open(encoding="utf-8-sig") as source:
        for line_number, line in enumerate(source, start=1):
            code = line.strip()
            if not code:
                continue
            if len(code) > 512 or any(ord(character) < 32 for character in code):
                raise ValueError(f"الكود بالسطر {line_number} طويل أو يحتوي محارف تحكّم.")
            result = db.execute(
                """
                INSERT INTO codes (fingerprint, stage, code) VALUES (?, ?, ?)
                ON CONFLICT (fingerprint) DO NOTHING
                """,
                (fingerprint(code), stage, code),
            )
            added += result.rowcount
            skipped += 1 - result.rowcount
    return added, skipped


def claim_code(db, stage, chat_id, message_id):
    with db:
        claim = db.execute(
            "INSERT INTO claims VALUES (?, ?) ON CONFLICT DO NOTHING",
            (chat_id, message_id),
        )
        if claim.rowcount == 0:
            return "claimed", None
        row = db.execute(
            """
            SELECT id, code FROM codes
            WHERE stage = ? AND code IS NOT NULL ORDER BY id LIMIT 1
            """,
            (stage,),
        ).fetchone()
        if row is None:
            db.execute(
                "DELETE FROM claims WHERE chat_id = ? AND message_id = ?",
                (chat_id, message_id),
            )
            return "empty", None
        code_id, code = row
        db.execute("UPDATE codes SET code = NULL WHERE id = ?", (code_id,))
        return "reserved", (code_id, code)


def restore_code(db, chat_id, message_id, code_id, code):
    with db:
        db.execute("UPDATE codes SET code = ? WHERE id = ?", (code, code_id))
        db.execute(
            "DELETE FROM claims WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )


class TelegramError(Exception):
    def __init__(self, method, status=None, rejected=False):
        super().__init__(f"{method} ({status or 'unconfirmed'})")
        self.status = status
        self.rejected = rejected


class Telegram:
    def __init__(self, token):
        self.base_url = f"https://api.telegram.org/bot{token}/"

    def call(self, method, **params):
        request = Request(
            self.base_url + method,
            data=json.dumps(params).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        status = None
        try:
            try:
                with urlopen(request, timeout=40) as response:
                    body = response.read()
                    status = response.status
            except HTTPError as error:
                with error:
                    body = error.read()
                    status = error.code
            result = json.loads(body)
        except (URLError, OSError, ValueError) as error:
            raise TelegramError(method, status) from error

        if isinstance(result, dict) and result.get("ok") is True and "result" in result:
            return result["result"]
        rejected = (
            status is not None
            and 400 <= status < 500
            and isinstance(result, dict)
            and result.get("ok") is False
            and result.get("error_code") == status
        )
        raise TelegramError(method, status, rejected)


def answer_callback(api, query, text=None):
    params = {"callback_query_id": query["id"]}
    if text:
        params.update(text=text, show_alert=True)
    try:
        api.call("answerCallbackQuery", **params)
    except TelegramError as error:
        logging.warning("%s", error)


def handle_update(api, db, update):
    message = update.get("message")
    if message:
        chat = message["chat"]
        command = message.get("text", "").split(maxsplit=1)
        if chat["type"] == "private" and command and command[0].split("@")[0] == "/start":
            api.call(
                "sendMessage",
                chat_id=chat["id"],
                text="أهلاً! اضغط «ابدأ» حتى تختار مرحلتك.",
                reply_markup=START_MENU,
            )
        return

    query = update.get("callback_query")
    if not query:
        return
    message = query.get("message")
    if not message or message["chat"]["type"] != "private":
        answer_callback(api, query, "افتح البوت بالمحادثة الخاصة.")
        return
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]
    action = query.get("data")

    if action == "start":
        try:
            api.call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text="اختار المرحلة حتى تستلم كود واحد.",
                reply_markup=STAGE_MENU,
            )
        finally:
            answer_callback(api, query)
        return

    stage_actions = {f"stage:{stage}": stage for stage in STAGES}
    if action not in stage_actions:
        answer_callback(api, query)
        return

    stage = stage_actions[action]
    status, reservation = claim_code(db, stage, chat_id, message_id)
    if status == "claimed":
        answer_callback(api, query, "هذا الاختيار استُخدم. أرسل /start حتى تبدأ من جديد.")
        return
    if status == "empty":
        answer_callback(api, query, "أكواد هاي المرحلة خلصت. اختار مرحلة ثانية أو جرّب بعدين.")
        return

    code_id, code = reservation
    try:
        api.call(
            "sendMessage",
            chat_id=chat_id,
            text=f"كود المرحلة {STAGE_NAMES[stage - 1]}\n\n<code>{html.escape(code)}</code>",
            parse_mode="HTML",
        )
    except TelegramError as error:
        if error.rejected:
            restore_code(db, chat_id, message_id, code_id, code)
            answer_callback(api, query, "تعذّر إرسال الكود. جرّب مرة ثانية.")
        else:
            answer_callback(api, query, "ما قدرنا نتأكد من الإرسال. شيّك المحادثة.")
        logging.warning("%s", error)
    else:
        answer_callback(api, query)


def run(api, db):
    offset = 0
    logging.info("البوت يشتغل. للإيقاف اضغط Ctrl+C.")
    while True:
        try:
            updates = api.call(
                "getUpdates",
                offset=offset,
                timeout=25,
                allowed_updates=["message", "callback_query"],
            )
        except TelegramError as error:
            if error.status in (401, 404, 409):
                raise
            logging.warning("%s", error)
            time.sleep(5)
            continue
        for update in updates:
            try:
                handle_update(api, db, update)
            except (TelegramError, sqlite3.Error) as error:
                logging.error("%s", error)
            offset = update["update_id"] + 1


def main():
    parser = argparse.ArgumentParser(description="بوت توزيع أكواد تليگرام على خمس مراحل")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="تشغيل البوت")
    commands.add_parser("stock", help="عرض عدد الأكواد المتوفرة")
    add = commands.add_parser("add", help="استيراد أكواد من ملف نصي")
    add.add_argument("stage", type=int, choices=STAGES)
    add.add_argument("file", type=Path)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if args.command == "run" and not token:
        parser.exit(1, "عيّن TELEGRAM_BOT_TOKEN قبل تشغيل البوت.\n")

    try:
        with closing(open_database(args.db)) as db:
            if args.command == "add":
                added, skipped = import_codes(db, args.stage, args.file)
                print(f"أُضيف {added} كود. تم تجاوز {skipped} كود مكرر.")
            elif args.command == "stock":
                counts = dict(db.execute(
                    "SELECT stage, COUNT(*) FROM codes WHERE code IS NOT NULL GROUP BY stage"
                ))
                for stage in STAGES:
                    print(f"المرحلة {stage} — {counts.get(stage, 0)} كود متوفر")
            else:
                run(Telegram(token), db)
    except KeyboardInterrupt:
        return
    except (TelegramError, sqlite3.Error, OSError, ValueError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
