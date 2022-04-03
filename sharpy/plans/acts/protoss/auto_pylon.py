from math import ceil

from sharpy.plans.acts import GridBuilding
from sc2.ids.unit_typeid import UnitTypeId


class AutoPylon(GridBuilding):
    """Builds pylons automatically when needed based on predicted supply growth speed."""

    def __init__(self):
        super().__init__(UnitTypeId.PYLON, 0)

    async def execute(self):
        self.to_count = await self.pylon_count_calc()
        return await super().execute()

    async def pylon_count_calc(self) -> int:
        pylon_build_duration = self.knowledge.ai.calculate_cost(UnitTypeId.PYLON).time
        lookahead = pylon_build_duration + 3  # 2 seconds worker travel/buffer time
        supply_prediction = self.ai.supply_used  # predicted in the next `lookahead` seconds

        # Nexus probes
        for production_structure in self.cache.own(UnitTypeId.NEXUS):
            if self.time_till_idle(production_structure) < lookahead:
                supply_prediction += self.knowledge.ai.calculate_supply_cost(UnitTypeId.PROBE)
        # Nexus mothership
        if self.cache.own(UnitTypeId.FLEETBEACON) and not self.cache.own(UnitTypeId.MOTHERSHIP):
            supply_prediction += self.knowledge.ai.calculate_supply_cost(UnitTypeId.MOTHERSHIP)
        # Gateway
        for production_structure in self.cache.own(UnitTypeId.GATEWAY):
            if self.time_till_idle(production_structure) < lookahead:
                # all gateway units are 2 supply
                supply_prediction += self.knowledge.ai.calculate_supply_cost(UnitTypeId.ZEALOT)
        # Warp Gate - cannot tell cooldown progress easily, so we always assume they will be ready
        for _ in self.cache.own(UnitTypeId.WARPGATE):
            # all gateway units are 2 supply
            supply_prediction += self.knowledge.ai.calculate_supply_cost(UnitTypeId.ZEALOT)
        # Robotics Facility
        for production_structure in self.cache.own(UnitTypeId.ROBOTICSFACILITY):
            if self.time_till_idle(production_structure) < lookahead:
                if self.cache.own(UnitTypeId.ROBOTICSBAY):
                    supply_prediction += self.knowledge.ai.calculate_supply_cost(UnitTypeId.COLOSSUS)
                else:
                    supply_prediction += self.knowledge.ai.calculate_supply_cost(UnitTypeId.IMMORTAL)
        # Stargate
        for production_structure in self.cache.own(UnitTypeId.STARGATE):
            if self.time_till_idle(production_structure) < lookahead:
                if self.cache.own(UnitTypeId.FLEETBEACON):
                    supply_prediction += self.knowledge.ai.calculate_supply_cost(UnitTypeId.CARRIER)
                else:
                    supply_prediction += self.knowledge.ai.calculate_supply_cost(UnitTypeId.VOIDRAY)

        # correct for maxed
        supply_prediction = min(supply_prediction, 200)

        # correct for Nexus finishing soon
        nexus_count: int = self.cache.own(UnitTypeId.NEXUS).ready.amount
        for nexus_in_progress in self.cache.own(UnitTypeId.NEXUS).not_ready:
            if self.time_till_idle(nexus_in_progress) < lookahead:
                nexus_count += 1
        pylon_to_count = ceil((supply_prediction - 15 * nexus_count) / 8)
        return pylon_to_count
