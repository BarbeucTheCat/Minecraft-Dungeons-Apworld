from test.bases import WorldTestBase


class MCDungeonsTestBase(WorldTestBase):
    """Base class for Minecraft Dungeons world tests - imported by every
    other module in this package. Each test method that uses this (or a
    subclass of this) gets its own solo MultiWorld generated and torn
    down, running the default WorldTestBase checks (test_fill,
    test_all_state_can_reach_everything, test_empty_state_can_reach_
    something) alongside anything defined on the subclass itself - see
    tests.md."""
    game = "Minecraft Dungeons"
