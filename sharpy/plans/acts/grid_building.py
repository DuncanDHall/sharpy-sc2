from math import floor
from typing import Optional, Set

from sc2.data import Race
from sc2.ids.ability_id import AbilityId
from sc2.pixel_map import PixelMap
from sharpy.sc2math import to_new_ticks

from sharpy.managers.core.roles import UnitTask
from sharpy.utils import map_to_point2s_center
from sc2.ids.unit_typeid import UnitTypeId
from sc2.position import Point2
from sc2.unit import Unit
from sc2.constants import ZERG_TECH_REQUIREMENT, TERRAN_TECH_REQUIREMENT, PROTOSS_TECH_REQUIREMENT

from .act_building import ActBuilding
from sharpy.interfaces import IBuildingSolver, IIncomeCalculator
from sharpy.managers.core import PathingManager

worker_trainers = {AbilityId.NEXUSTRAIN_PROBE, AbilityId.COMMANDCENTERTRAIN_SCV}


class WorkerStuckStatus:
    def __init__(self):
        self.tag_stuck: Optional[int] = None
        self.last_move_detected_time: Optional[float] = None
        self.current_tag: Optional[int] = None
        self.last_moved_position: Optional[Point2] = None
        self.last_iteration_asked = 0

    def need_new_worker(self, current_worker: Unit, time: float, target: Point2, iteration: int) -> bool:
        if self.last_iteration_asked < iteration - 1:
            # reset
            self.tag_stuck = None
            self.current_tag = current_worker.tag
            self.last_move_detected_time = time
            self.last_moved_position = current_worker.position
            return False

        self.last_iteration_asked = iteration

        if current_worker.tag == self.current_tag:
            if target.distance_to_point2(current_worker.position) < 2.5:
                return False  # Worker is close enough to destination, not stuck
            if self.last_moved_position is None:
                self.last_moved_position = current_worker.position
            elif self.last_moved_position.distance_to_point2(current_worker.position) > 0.5:
                self.last_move_detected_time = time
                self.last_moved_position = current_worker.position
            elif time - self.last_move_detected_time > 1:
                self.tag_stuck = self.current_tag
                return True
        elif self.tag_stuck == current_worker.tag:
            return True

        # reset
        self.tag_stuck = None
        self.current_tag = current_worker.tag
        self.last_move_detected_time = time
        self.last_moved_position = current_worker.position
        return False


class GridBuilding(ActBuilding):

    building_solver: IBuildingSolver
    income_calculator: IIncomeCalculator
    pather: Optional[PathingManager]

    def __init__(
        self,
        unit_type: UnitTypeId,
        to_count: int = 1,
        iterator: Optional[int] = None,
        priority: bool = False,
        allow_wall: bool = True,
        consider_worker_production: bool = True,
        override_reserved: bool = False
    ):
        super().__init__(unit_type, to_count)
        self.allow_wall = allow_wall
        assert isinstance(priority, bool)
        self.priority = priority
        self.builder_tag: Optional[int] = None
        self.iterator: Optional[int] = iterator
        self.consider_worker_production = consider_worker_production
        self.building_solver: IBuildingSolver = None
        self.make_pylon = None
        self.worker_stuck: WorkerStuckStatus = WorkerStuckStatus()
        self.override_reserved = override_reserved

    async def start(self, knowledge: "Knowledge"):
        await super().start(knowledge)
        self.building_solver = self.knowledge.get_required_manager(IBuildingSolver)
        self.pather = self.knowledge.get_manager(PathingManager)
        self.income_calculator = self.knowledge.get_required_manager(IIncomeCalculator)
        if self.unit_type != UnitTypeId.PYLON:
            self.make_pylon: Optional[GridBuilding] = GridBuilding(
                UnitTypeId.PYLON, 0, 2, override_reserved=self.priority)
            await self.make_pylon.start(knowledge)

    async def execute(self) -> bool:
        existing_count = self.get_count(self.unit_type, include_pending=False, include_not_ready=True)

        # check if done
        if existing_count >= self.to_count:
            self.clear_worker()
            return True  # Step is done

        # manage worker role
        if self.builder_tag:
            self.roles.set_task(UnitTask.Building, self.cache.by_tag(self.builder_tag))

        # verify prerequisites in progress
        if self.knowledge.prerequisite_progress(self.unit_type) <= 0.0:
            return False

        # check if enough are pending
        if existing_count + self.pending_build(self.unit_type) >= self.to_count:
            return False

        # find a position
        if self.knowledge.my_race == Race.Protoss:
            position = self.position_protoss(existing_count)
        elif self.knowledge.my_race == Race.Terran:
            position = self.position_terran(existing_count)
        else:
            position = self.position_zerg(existing_count)
        if position is None:
            self.print(f"Can't find free position to build {self.unit_type.name} in!")
            return False  # Stuck and cannot proceed

        # check for designated worker builder
        worker = None
        if self.builder_tag is not None:
            worker = self.cache.by_tag(self.builder_tag)
            if worker is None:
                self.builder_tag = None
        # designate new worker
        if worker is None:
            worker = self.get_worker_builder(position)
        # no workers
        if worker is None:
            return False  # Cannot proceed

        # check worker is stuck
        if self.worker_stuck.need_new_worker(worker, self.ai.time, position, self.knowledge.iteration):
            self.print(f"Worker {worker.tag} was found stuck!")
            self.roles.set_task(UnitTask.Reserved, worker)  # Set temp reserved for the stuck worker.
            self.clear_worker()
            return False

        # try to build
        if (
                # can afford
                self.knowledge.can_afford(self.unit_type, check_supply_cost=False, override_reserved=self.override_reserved)
                # tech complete
                and self.knowledge.prerequisite_progress(self.unit_type) >= 1
                # No duplicate builds
                and worker.tag not in self.ai.unit_tags_received_action
        ):
                if self.knowledge.my_race == Race.Protoss:
                    await self.build_protoss(worker, existing_count, position)
                elif self.knowledge.my_race == Race.Terran:
                    await self.build_terran(worker, existing_count, position)
                else:
                    await self.build_zerg(worker, existing_count, position)

        # pre-move worker and reserve resources
        if self.priority and not self.has_build_order(worker):
            self.pre_move_worker(worker, position)

        return False

    def pre_move_worker(self, worker: Unit, position: Point2):
        # calculate earliest can build
        tech_wait_time = self.prerequisite_completion_time()
        d = worker.distance_to(position)
        travel_time: float = d / to_new_ticks(worker.movement_speed)
        cost = self.knowledge.ai.calculate_cost(self.unit_type)
        adjusted_income = self.income_calculator.mineral_income * 0.93  # 14 / 15 = 0.933333
        available_minerals = self.knowledge.ai.minerals if self.override_reserved else self.knowledge.available_minerals
        minerals_to_collect = max(0.0, cost.minerals - available_minerals)
        minerals_wait_time = minerals_to_collect / max(adjusted_income, 0.01) - travel_time
        available_gas = self.knowledge.ai.vespene if self.override_reserved else self.knowledge.available_gas
        gas_to_collect = max(0.0, cost.vespene - available_gas)
        gas_wait_time = gas_to_collect / max(self.income_calculator.gas_income, 0.01) - travel_time
        build_wait_time = max(travel_time, tech_wait_time, minerals_wait_time, gas_wait_time)

        self.knowledge.reserve(cost.minerals, cost.vespene, build_wait_time, self.unit_type)

        if build_wait_time < travel_time + 0.1:
            self.set_worker(worker)
            worker.move(self.adjust_build_to_move(position))

    def adjust_build_to_move(self, position: Point2) -> Point2:
        closest_zone: Optional[Point2] = None
        if self.pather:
            zone_index = self.pather.map.get_zone(position)
            if zone_index > 0:
                closest_zone = self.zone_manager.expansion_zones[zone_index - 1].center_location

        if closest_zone is None:
            closest_zone = position.closest(map_to_point2s_center(self.zone_manager.expansion_zones))

        return position.towards(closest_zone, 1)

    async def debug_actions(self):
        if self.builder_tag is not None:
            worker: Unit = self.cache.by_tag(self.builder_tag)

            if worker and worker.orders:
                moving_status = ""
                for order in worker.orders:
                    if moving_status != "":
                        moving_status += ", "
                    moving_status += order.ability.id.name
                self.client.debug_text_world(moving_status, worker.position3d)

    def set_worker(self, worker: Unit):
        self.roles.set_task(UnitTask.Building, worker)
        self.builder_tag = worker.tag

    def clear_worker(self):
        if self.builder_tag is not None:
            self.roles.clear_task(self.builder_tag)
            self.builder_tag = None

    def position_protoss(self, count) -> Optional[Point2]:
        is_pylon = self.unit_type == UnitTypeId.PYLON
        buildings = self.ai.structures
        matrix = self.ai.state.psionic_matrix
        future_position = None

        iterator = self.get_iterator(is_pylon, count)

        if is_pylon:
            for point in self.building_solver.buildings2x2[::iterator]:
                if not buildings.closer_than(1, point):
                    return point
        else:
            pylons = self.cache.own(UnitTypeId.PYLON).not_ready
            for point in self.building_solver.buildings3x3[::iterator]:
                if not self.allow_wall:
                    if point in self.building_solver.wall3x3:
                        continue
                if not buildings.closer_than(1, point) and matrix.covers(point):
                    return point

                if future_position is None and pylons and point.distance_to_closest(pylons) <= 7:
                    future_position = point

        return future_position

    def position_zerg(self, count) -> Optional[Point2]:
        buildings = self.ai.structures
        creep = self.ai.state.creep
        future_position = None

        for point in self.building_solver.buildings3x3:
            if not buildings.closer_than(1, point) and self.is_on_creep(creep, point):
                return point

        return future_position

    def position_terran(self, count) -> Optional[Point2]:
        is_depot = self.unit_type == UnitTypeId.SUPPLYDEPOT
        buildings = self.ai.structures
        future_position = None

        if is_depot:
            for point in self.building_solver.buildings2x2:
                if not buildings.closer_than(1, point):
                    return point
        else:
            pylons = self.cache.own(UnitTypeId.PYLON).not_ready
            reserved_landing_locations: Set[Point2] = set(self.building_solver.structure_target_move_location.values())
            for point in self.building_solver.buildings3x3:
                if not self.allow_wall:
                    if point in self.building_solver.wall3x3:
                        continue
                # If a structure is landing here from AddonSwap() then dont use this location
                if point in reserved_landing_locations:
                    continue
                # If this location has a techlab or reactor next to it, then don't create a new structure here
                if point in self.building_solver.free_addon_locations:
                    continue
                if not buildings.closer_than(1, point):
                    return point

                if future_position is None and pylons and point.distance_to_closest(pylons) <= 7:
                    future_position = point

        return future_position

    def get_iterator(self, is_pylon, count):
        if self.iterator is None:
            if is_pylon and count < 14:
                return 2
            return 1

        return self.iterator

    async def build_protoss(self, worker: Unit, count, position: Point2):
        if self.unit_type is not UnitTypeId.PYLON:
            if not self.ai.state.psionic_matrix.covers(position):
                return
        if self.has_build_order(worker):
            # TODO: is this correct?
            self.set_worker(worker)
            worker.build(self.unit_type, position, queue=True)

        # TODO: Remake the error handling with frame delay
        self.set_worker(worker)
        worker.build(self.unit_type, position)

    async def build_zerg(self, worker: Unit, count, position: Point2):
        # try the selected position first
        # TODO: Remake the error handling with frame delay
        self.set_worker(worker)
        worker.build(self.unit_type, position)

    async def build_terran(self, worker: Unit, count, position: Point2):
        # try the selected position first
        # TODO: Remake the error handling with frame delay
        self.set_worker(worker)
        worker.build(self.unit_type, position)

    def is_on_creep(self, creep: PixelMap, point: Point2) -> bool:
        x_original = floor(point.x) - 1
        y_original = floor(point.y) - 1
        for x in range(x_original, x_original + 5):
            for y in range(y_original, y_original + 5):
                if not creep.is_set(Point2((x, y))):
                    return False
        return True

    def prerequisite_completion_time(self) -> float:
        """ Return progress in realtime seconds """
        # Protoss:
        if self.unit_type == UnitTypeId.GATEWAY or self.unit_type == UnitTypeId.FORGE:
            return self.building_progress(UnitTypeId.PYLON)

        if self.unit_type == UnitTypeId.CYBERNETICSCORE:
            return min(self.building_progress(UnitTypeId.GATEWAY), self.building_progress(UnitTypeId.WARPGATE))

        if self.unit_type == UnitTypeId.TWILIGHTCOUNCIL:
            return self.building_progress(UnitTypeId.CYBERNETICSCORE)

        if self.unit_type == UnitTypeId.TEMPLARARCHIVE:
            return self.building_progress(UnitTypeId.TWILIGHTCOUNCIL)

        if self.unit_type == UnitTypeId.DARKSHRINE:
            return self.building_progress(UnitTypeId.TWILIGHTCOUNCIL)

        if self.unit_type == UnitTypeId.STARGATE:
            return self.building_progress(UnitTypeId.CYBERNETICSCORE)

        if self.unit_type == UnitTypeId.FLEETBEACON:
            return self.building_progress(UnitTypeId.STARGATE)

        if self.unit_type == UnitTypeId.ROBOTICSFACILITY:
            return self.building_progress(UnitTypeId.CYBERNETICSCORE)

        if self.unit_type == UnitTypeId.ROBOTICSBAY:
            return self.building_progress(UnitTypeId.ROBOTICSFACILITY)

        if self.unit_type == UnitTypeId.PHOTONCANNON:
            return self.building_progress(UnitTypeId.FORGE)

        if self.unit_type == UnitTypeId.SHIELDBATTERY:
            return self.building_progress(UnitTypeId.CYBERNETICSCORE)

        # Terran:
        if self.unit_type == UnitTypeId.BARRACKS:
            return self.building_progress(UnitTypeId.SUPPLYDEPOT)
        if self.unit_type == UnitTypeId.FACTORY:
            return self.building_progress(UnitTypeId.BARRACKS)
        if self.unit_type == UnitTypeId.ARMORY:
            return self.building_progress(UnitTypeId.FACTORY)
        if self.unit_type == UnitTypeId.STARPORT:
            return self.building_progress(UnitTypeId.FACTORY)
        if self.unit_type == UnitTypeId.FUSIONCORE:
            return self.building_progress(UnitTypeId.STARPORT)

        return 0
