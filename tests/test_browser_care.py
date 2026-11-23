import asyncio

from friday import browser_care as care


class FakeStore(dict):
    def set(self, key, value):
        self[key] = value


def test_a_stuck_browser_restarts_only_when_idle_then_waits_out_the_cooldown():
    now, busy, repairs, told = [1000.0], [True], [], []

    async def stuck():
        return care.Probe(False, why="timeout")

    async def is_busy(me):
        return busy[0]

    async def repair(me):
        repairs.append(me)
        return ""

    async def tell(text):
        told.append(text)

    doctor = care.Doctor(repair, is_busy, tell, FakeStore(), check=stuck, clock=lambda: now[0])

    async def scenario():
        assert not await doctor.ensure("lock")  # two failures, but work is running: left alone
        assert repairs == [] and told == []
        busy[0] = False
        assert await doctor.check()  # the work ended: one restart, one line, and it says what may have failed
        assert repairs == [""] and len(told) == 1 and "may have failed" in told[0]
        now[0] += care.CHECK_FRESH + 1
        for _ in range(4):  # stuck again right away: no restart loop, one notice
            assert not await doctor.check()
        assert repairs == [""] and len(told) == 2
        now[0] += care.RESTART_COOLDOWN
        assert await doctor.check() and repairs == ["", ""]

    asyncio.run(scenario())


def test_cleanup_keeps_what_is_still_in_use():
    now, idle = 10_000.0, 1200
    lease = lambda who, tabs, at: {"owner": who, "tabs": tabs, "at": at}
    held = {
        care.LEASE + "chat:1": lease("chat:1", ["recent"], now - 60),  # a follow-up may need the page
        care.LEASE + "chat:2": lease("chat:2", ["long-turn"], now - 5000),  # its turn is still running
        care.LEASE + "chat:3": lease("chat:3", ["idle", "popup"], now - idle),
        care.RELEASED + "a": lease("run:7", ["finished", "video"], now),  # released: due at once
    }
    live = {t: "https://x/" for x in held.values() for t in x["tabs"]} | {"restored": "https://old/", "blank": "about:blank"}
    close, over = care.plan_cleanup(held, live, {"video"}, {"chat:2"}, idle, now)
    assert close == {"idle", "popup", "finished", "restored"}
    assert over == [care.LEASE + "chat:3"]  # the run's lease stays until its video has stopped
