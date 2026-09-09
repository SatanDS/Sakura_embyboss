"""Offline regressions using source ASTs and an isolated SQLite database."""

import ast
import asyncio
import logging
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from sqlalchemy import BigInteger, Column, DateTime, Integer, String, create_engine
from sqlalchemy.orm import declarative_base, sessionmaker


ROOT = Path(__file__).resolve().parents[1]


def load_definitions(relative_path, names, namespace):
    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8-sig"))
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            node.decorator_list = []
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id in names for target in node.targets):
            nodes.append(node)
    exec(compile(ast.Module(body=nodes, type_ignores=[]), relative_path, "exec"), namespace)
    return namespace


class InviteTransactionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine("sqlite:///" + str(Path(self.temp.name) / "test.db"))
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.env = dict(
            Base=declarative_base(), Column=Column, BigInteger=BigInteger, String=String,
            DateTime=DateTime, Integer=Integer, Session=self.session_factory,
        )
        load_definitions("bot/sql_helper/sql_emby.py", {"Emby"}, self.env)
        load_definitions("bot/sql_helper/sql_code.py",
                         {"Code", "sql_buy_invite_codes", "INVITE_DURATIONS", "MAX_INVITE_CODES"}, self.env)
        self.env["Base"].metadata.create_all(self.engine)
        self.Emby, self.Code = self.env["Emby"], self.env["Code"]
        with self.session_factory() as session:
            session.add(self.Emby(tg=1, iv=100, lv="b"))
            session.commit()

    def tearDown(self):
        self.engine.dispose()
        self.temp.cleanup()

    def state(self):
        with self.session_factory() as session:
            return session.get(self.Emby, 1).iv, session.query(self.Code).count()

    def buy(self, codes, cost=60, days=30, eligible=lambda user: True):
        return self.env["sql_buy_invite_codes"](1, codes, days, cost, eligible)

    def test_code_insert_failure_rolls_back_charge(self):
        with self.session_factory() as session:
            session.add(self.Code(code="duplicate", tg=1, us=30))
            session.commit()
        self.assertEqual(self.buy(["duplicate"])["status"], "error")
        self.assertEqual(self.state(), (100, 1))

    def test_two_concurrent_purchases_cannot_spend_same_balance(self):
        barrier = threading.Barrier(2)

        def buy(index):
            barrier.wait(timeout=5)
            return self.buy(["concurrent-" + str(index)])

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(buy, [1, 2]))
        self.assertEqual(sorted(result["status"] for result in results), ["insufficient", "ok"])
        self.assertEqual(self.state(), (40, 1))

    def test_current_balance_and_eligibility_are_checked(self):
        with self.session_factory() as session:
            session.get(self.Emby, 1).iv = 10
            session.commit()
        self.assertEqual(self.buy(["fresh"])["status"], "insufficient")
        self.assertEqual(self.buy(["forbidden"], 5, eligible=lambda user: False)["status"], "forbidden")
        self.assertEqual(self.state(), (10, 0))

    def test_invalid_transaction_arguments_do_not_charge(self):
        for codes, days, cost in [([], 30, 1), (["x"], -1, 1), (["x"], 30, -1),
                                  ([str(i) for i in range(101)], 30, 1)]:
            with self.subTest(days=days, cost=cost, count=len(codes)):
                self.assertEqual(self.buy(codes, cost, days)["status"], "invalid")
                self.assertEqual(self.state(), (100, 0))


class InviteInputTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.user = SimpleNamespace(iv=100, lv="b")
        self.messages = []
        self.buys = []
        self.content = SimpleNamespace(text="", delete=AsyncMock(), from_user=SimpleNamespace(id=1))

        async def reply(*args, **kwargs):
            self.messages.append((args, kwargs))
            return True

        def buy(*args):
            self.buys.append(args)
            return {"status": "ok", "balance": 90}

        self.env = dict(
            asyncio=asyncio, math=__import__("math"),
            _open=SimpleNamespace(invite=True, invite_lv="b", invite_cost=10),
            sql_get_emby=lambda **kwargs: self.user, invite_policy_allows=lambda *args: True,
            callAnswer=reply, editMessage=reply, sendMessage=reply, do_store=reply,
            callListen=AsyncMock(side_effect=lambda *args: self.content),
            sql_update_emby=lambda *args, **kwargs: setattr(self.user, "iv", kwargs["iv"]),
            cr_link_one=AsyncMock(return_value="code"), sql_buy_invite_codes=buy,
            pwd_create=AsyncMock(return_value="abcdefghij"), Emby=SimpleNamespace(tg=1),
            ExDate=lambda: SimpleNamespace(mon=30, unused=-1, code="code", link="link"),
            ranks=SimpleNamespace(logo="Test"), bot_name="testbot", sakura_b="coins",
            LOGGER=logging.getLogger("business-test"),
        )
        load_definitions("bot/sql_helper/sql_code.py", {"INVITE_DURATIONS", "MAX_INVITE_CODES"}, self.env)
        load_definitions("bot/modules/panel/member_panel.py", {"_parse_invite_purchase", "do_store_invite"}, self.env)

    async def test_invalid_input_never_changes_balance_or_creates_codes(self):
        for text in ["mon -1 code", "mon 0 code", "mon 101 code", "unused 30 code",
                     "used 1 code", "mon 1 unused", "mon 1 unknown", "mon nan code", "mon 1"]:
            with self.subTest(text=text):
                self.user.iv = 100
                self.content.text = text
                await self.env["do_store_invite"](None, SimpleNamespace(from_user=SimpleNamespace(id=1)))
                self.assertEqual(self.user.iv, 100)
                self.assertEqual(self.buys, [])

    async def test_valid_code_and_link_purchase_use_atomic_helper(self):
        self.user.iv = 1000
        for mode in ["code", "link"]:
            with self.subTest(mode=mode):
                self.content.text = "year 1 " + mode
                await self.env["do_store_invite"](None, SimpleNamespace(from_user=SimpleNamespace(id=1)))
                self.assertEqual(self.buys[-1][1:4], (["Test-365-Register_abcdefghij"], 365, 121))
                expected = "t.me/testbot?start=" if mode == "link" else "`Test-365-Register_"
                self.assertTrue(any(expected in str(message) for message in self.messages))


class RenewalTests(unittest.IsolatedAsyncioTestCase):
    async def run_renewal(self, non_tg, policy_result=True, database_result=True, days=30, level="c"):
        self.updates = []
        self.reply = SimpleNamespace(edit=AsyncMock(return_value=SimpleNamespace(forward=AsyncMock())))
        self.user = SimpleNamespace(name="test", lv=level, embyid="emby-1", ex=datetime.now() - timedelta(days=1))
        if not non_tg:
            self.user.tg = 1

        def update(*args, **kwargs):
            self.updates.append(kwargs)
            return database_result

        env = dict(datetime=datetime, timedelta=timedelta,
                   get_user_input=AsyncMock(return_value=(days, self.user, 1 if non_tg else None, "admin")),
                   sendMessage=AsyncMock(), emby=SimpleNamespace(emby_change_policy=AsyncMock(return_value=policy_result)),
                   sql_update_emby=update, sql_update_emby2=update, Emby=SimpleNamespace(tg=1),
                   Emby2=SimpleNamespace(embyid="emby-1"), LOGGER=logging.getLogger("business-test"))
        load_definitions("bot/modules/commands/renew.py", {"renew_user"}, env)
        await env["renew_user"](None, SimpleNamespace(reply=AsyncMock(return_value=self.reply)))
        return env

    async def test_failed_remote_enable_does_not_update_database_or_claim_success(self):
        for non_tg in [False, True]:
            with self.subTest(non_tg=non_tg):
                await self.run_renewal(non_tg, policy_result=False)
                self.assertEqual(self.updates, [])
                self.assertNotIn("已调整", str(self.reply.edit.call_args))

    async def test_success_updates_tg_and_non_tg_level(self):
        for non_tg in [False, True]:
            with self.subTest(non_tg=non_tg):
                await self.run_renewal(non_tg)
                self.assertEqual(self.updates[0]["lv"], "b")
                self.assertGreater(self.updates[0]["ex"], datetime.now())
                if non_tg:
                    self.assertEqual(self.updates[0]["expired"], 0)

    async def test_database_failure_is_not_reported_as_success(self):
        await self.run_renewal(True, database_result=False)
        self.assertNotIn("已调整", str(self.reply.edit.call_args))

    async def test_negative_renewal_disables_account(self):
        env = await self.run_renewal(True, days=-1, level="b")
        env["emby"].emby_change_policy.assert_awaited_once_with(emby_id="emby-1", disable=True)
        self.assertEqual(self.updates[0]["lv"], "c")
        self.assertEqual(self.updates[0]["expired"], 1)


class RedEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_equal_envelopes_conserve_full_amount(self):
        for money, count in [(5, 2), (10, 3), (100, 7), (10, 5), (5, 1)]:
            with self.subTest(money=money, count=count):
                balances = {uid: 0 for uid in range(count)}

                class UserColumn:
                    def __eq__(self, other):
                        return other

                def update(uid, **kwargs):
                    balances[uid] = kwargs["iv"]
                    return True

                env = dict(sql_get_emby=lambda tg: SimpleNamespace(iv=balances[tg]), sql_update_emby=update,
                           Emby=SimpleNamespace(tg=UserColumn()), callAnswer=AsyncMock(), editMessage=AsyncMock(),
                           MAX_INT_VALUE=2**31 - 1, MIN_INT_VALUE=-(2**31), sakura_b="coins", red_envelopes={},
                           generate_final_message=AsyncMock(return_value="done"))
                load_definitions("bot/modules/extra/red_envelope.py", {"RedEnvelope", "grab_red_envelope"}, env)
                envelope = env["RedEnvelope"](money, count, 99, "sender", "equal")
                env["red_envelopes"]["test"] = envelope
                for uid in balances:
                    call = SimpleNamespace(data="red_envelope-test", from_user=SimpleNamespace(id=uid, first_name="test"))
                    await env["grab_red_envelope"](None, call)
                self.assertEqual(sum(balances.values()), money)
                self.assertEqual(envelope.rest_money, 0)
                self.assertNotIn("test", env["red_envelopes"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
