import math

from sc2.data import Race
from sc2.ids.ability_id import AbilityId
from sharpy.interfaces import IIncomeCalculator
from sharpy.knowledges import Knowledge
from sharpy.plans.acts import ActBase
from sc2.ids.unit_typeid import UnitTypeId
from sc2.game_data import AbilityData, Cost
from sc2.unit import Unit, UnitOrder


class Workers(ActBase):

    """
    Builds workers in an optimal way for Protoss and Terran.
    Does not function for Zerg!
    Does not consider chrono boost.
    """

    income_calculator: IIncomeCalculator

    def __init__(self, to_count: int = 80, priority: bool = True, override_reserved: bool = False):
        super().__init__()
        self.unit_type: UnitTypeId = None
        self.to_count = to_count
        self.ability: AbilityId = None
        self.cost: Cost = None
        self.priority = priority  # Reserve minerals
        self.override_reserved = override_reserved  # disallow unplanned worker cuts if True

    async def start(self, knowledge: Knowledge):
        await super().start(knowledge)
        assert knowledge.my_worker_type in {UnitTypeId.PROBE, UnitTypeId.SCV}
        self.unit_type = knowledge.my_worker_type
        unit_data = self.ai._game_data.units[self.unit_type.value]
        ability_data: AbilityData = unit_data.creation_ability
        self.ability = ability_data.id
        self.cost = ability_data.cost

    async def execute(self) -> bool:
        count_to_make = self.to_count - self.get_count(self.unit_type, include_pending=True, include_not_ready=True)

        builders = self.ai.townhalls.ready
        for builder in builders:
            if count_to_make <= 0:
                return True
            time_till_idle = sum([order.ability.cost.time / 22.4 * (1 - order.progress) for order in builder.orders])
            # try to train
            if (
                    time_till_idle < 0.1
                    and self.knowledge.can_afford(self.unit_type, override_reserved=self.override_reserved)
                    and not builder.is_flying
                    and self.knowledge.cooldown_manager.is_ready(builder.tag, self.ability)
            ):
                if builder.train(self.unit_type, queue=True):
                    self.print(f"{self.unit_type.name} from {builder.type_id.name} at {builder.position}")
                    count_to_make -= 1
            # reserve resources (approximately correct)
            if self.priority:
                count_builder_will_make = int(count_to_make / len(builders)) + 1  # round up
                for w in range(0, count_builder_will_make):
                    self.knowledge.reserve_costs(self.unit_type, time_till_idle + w * self.cost.time)

        return False
