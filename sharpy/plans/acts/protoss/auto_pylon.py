from math import ceil
from typing import Optional

from sharpy.interfaces import IUnitCache
from sharpy.knowledges import Knowledge
from sharpy.plans.acts import GridBuilding
from sc2.ids.unit_typeid import UnitTypeId


class AutoPylon(GridBuilding):
    """Builds pylons automatically when needed based on predicted supply growth speed."""

    def __init__(self, override_reserved: bool = False):
        super().__init__(UnitTypeId.PYLON, 0, override_reserved=override_reserved)

    async def execute(self):
        self.to_count = await self.pylon_count_calc()
        return await super().execute()

    async def pylon_count_calc(self) -> int:
        pylon_build_duration = self.knowledge.ai.calculate_cost(UnitTypeId.PYLON).time
        buffer = 3
        lookahead = pylon_build_duration + buffer  # 3 seconds worker travel/buffer time

        # correct for maxed
        supply_prediction = min(self.predict_supply(self.knowledge, buffer), 200)

        # correct for Nexus finishing soon
        nexus_count: int = self.cache.own(UnitTypeId.NEXUS).ready.amount
        for nexus_in_progress in self.cache.own(UnitTypeId.NEXUS).not_ready:
            if self.knowledge.time_until_idle(nexus_in_progress) < lookahead:
                nexus_count += 1
        pylon_to_count = ceil((supply_prediction - 15 * nexus_count) / 8)
        return pylon_to_count

    @staticmethod
    def predict_supply(knowledge: Knowledge, time_buffer: float = 3.0, unit_cache: Optional[IUnitCache] = None):
        """Predicts the supply for one Pylon build time in the future, plus time_buffer, assuming constant production"""
        pylon_build_duration = knowledge.ai.calculate_cost(UnitTypeId.PYLON).time
        lookahead = pylon_build_duration + time_buffer
        supply_increase = 0
        if unit_cache is None:
            unit_cache = knowledge.unit_cache

        # Nexus probes
        for production_structure in unit_cache.own(UnitTypeId.NEXUS):
            if knowledge.time_until_idle(production_structure) < lookahead:
                supply_increase += knowledge.ai.calculate_supply_cost(UnitTypeId.PROBE)
        # Nexus mothership
        if unit_cache.own(UnitTypeId.FLEETBEACON) and not unit_cache.own(UnitTypeId.MOTHERSHIP):
            supply_increase += knowledge.ai.calculate_supply_cost(UnitTypeId.MOTHERSHIP)
        # Gateway
        for production_structure in unit_cache.own(UnitTypeId.GATEWAY):
            if knowledge.time_until_idle(production_structure) < lookahead:
                # all gateway units are 2 supply
                supply_increase += knowledge.ai.calculate_supply_cost(UnitTypeId.ZEALOT)
        # Warp Gate - cannot tell cooldown progress easily, so we always assume they will be ready
        for _ in unit_cache.own(UnitTypeId.WARPGATE):
            # all gateway units are 2 supply
            supply_increase += knowledge.ai.calculate_supply_cost(UnitTypeId.ZEALOT)
        # Robotics Facility
        for production_structure in unit_cache.own(UnitTypeId.ROBOTICSFACILITY):
            if knowledge.time_until_idle(production_structure) < lookahead:
                if unit_cache.own(UnitTypeId.ROBOTICSBAY):
                    supply_increase += knowledge.ai.calculate_supply_cost(UnitTypeId.COLOSSUS)
                else:
                    supply_increase += knowledge.ai.calculate_supply_cost(UnitTypeId.IMMORTAL)
        # Stargate
        for production_structure in unit_cache.own(UnitTypeId.STARGATE):
            if knowledge.time_until_idle(production_structure) < lookahead:
                if unit_cache.own(UnitTypeId.FLEETBEACON):
                    supply_increase += knowledge.ai.calculate_supply_cost(UnitTypeId.CARRIER)
                else:
                    supply_increase += knowledge.ai.calculate_supply_cost(UnitTypeId.VOIDRAY)

        return supply_increase + knowledge.ai.supply_used

