from collections import OrderedDict
from math import floor
from typing import Optional, Set, Tuple, List, Dict, Callable

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
    """Build buildings allowing the BuildingSolver to determine the locations

    Each position goes through the following steps:
    1. Unverified (will be checked and rejected if occupied, not on creep/psi-field, etc.)
    2. Planned (a building location has been set)
    3. Planned and Assigned (a worker has been assigned to the site)
    4. Building is pending (build order issued)
    5. Building started (building foundation has been laid)
    6. Building is completed
    """
    def __init__(
        self,
        unit_type: UnitTypeId,
        to_count: int = 1,
        priority: bool = False,
        allow_wall: bool = True,
        override_reserved: bool = False,
        multiple_builders: Optional[bool] = None
    ):
        """
        :param unit_type: unit type to build
        :param to_count: total count to build to (if to_count is 3 and you already have 1 then this will build 2 more)
        :param position_iterator: used to skip positions in building solver
        :param priority: if True will reserve resources and pre-move worker(s)
        :param allow_wall: whether this building should be placed in available wall spaces
        :param override_reserved: whether this build operation can spend reserved resources
        :param multiple_builders: whether to use multiple builders in parallel
        """
        super().__init__(unit_type, to_count)
        assert isinstance(priority, bool)
        self.priority = priority
        self.allow_wall = allow_wall
        self.override_reserved = override_reserved
        self.multiple_builders = multiple_builders

        self.worker_stuck: WorkerStuckStatus = WorkerStuckStatus()
        """For detecting workers that cannot move to their build site"""
        self.building_solver: Optional[IBuildingSolver] = None
        """Building solver to use"""
        self.potential_positions: Optional[List[Point2]] = None  # (position, assigned builder_tag)
        """Track all available positions for the building. Populated on start, and popped whenever a position is 
        found to be invalid"""
        self.active_builders: Set[int] = set()
        """Builders who are pre-moving, moving to pending buildings, and actively building"""
        self.planned_positions_workers: OrderedDict[Point2, Optional[int]] = OrderedDict()
        """Tracks which positions have been planned and if a worker has been assigned"""
        self.income_calculator: IIncomeCalculator = Optional[None]
        """For predicting how long it will take to gather the required resources"""
        self.pather: Optional[PathingManager] = None
        """For pathing the workers"""

    async def start(self, knowledge: "Knowledge"):
        await super().start(knowledge)
        if self.multiple_builders is None:
            if self.knowledge.my_race == Race.Protoss:
                self.multiple_builders = False
            else:
                self.multiple_builders = True
        self.building_solver = self.knowledge.get_required_manager(IBuildingSolver)
        self.income_calculator = self.knowledge.get_required_manager(IIncomeCalculator)
        self.pather = self.knowledge.get_manager(PathingManager)

    async def execute(self) -> bool:
        # verify prerequisites in progress
        if self.knowledge.prerequisite_progress(self.unit_type) <= 0.0:
            return False

        # check if done
        existing_count = self.get_count(self.unit_type, include_pending=False, include_not_ready=True)
        if existing_count >= self.to_count:
            return True  # Step is done

        # find and plan n valid positions
        if self.potential_positions is None:
            self.potential_positions = self.get_potential_positions()
        self.plan_positions()

        # remove inactive builder tags and roles
        for builder_tag in list(self.active_builders):
            worker = self.cache.by_tag(builder_tag)
            if worker is None or not self.has_build_order(worker):
                self.active_builders.remove(builder_tag)  # if pre-moving, will be marked active before role update
                self.roles.clear_task(builder_tag)

        # proceed with the planned positions
        for position in list(self.planned_positions_workers):

            # get worker unit
            worker = None
            if self.planned_positions_workers[position] is not None:
                worker = self.cache.by_tag(self.planned_positions_workers[position])
                # check worker died
                if worker is None:
                    self.planned_positions_workers[position] = None
                # check worker is stuck
                elif self.worker_stuck.need_new_worker(worker, self.ai.time, position, self.knowledge.iteration):
                    self.print(f"Worker {worker.tag} was found stuck at {worker.position}!")
                    self.roles.set_task(UnitTask.Reserved, worker)  # Set temp reserved for the stuck worker
                    self.planned_positions_workers[position] = None
            # use a temp worker
            if worker is None:
                worker = self.get_worker_builder(position)
            # no workers
            if worker is None:
                return False  # Cannot proceed

            # try to build
            if (
                    # can afford
                    self.knowledge.can_afford(self.unit_type, check_supply_cost=False,
                                              override_reserved=self.override_reserved)
                    # tech complete
                    and self.knowledge.prerequisite_progress(self.unit_type) >= 1
                    # No duplicate builds
                    and worker.tag not in self.ai.unit_tags_received_action
                    and not self.has_build_order(worker)
                    # Psionic matrix ready
                    and (
                        self.knowledge.my_race != Race.Protoss
                        or self.unit_type == UnitTypeId.PYLON
                        or self.ai.state.psionic_matrix.covers(position)
                    )
            ):
                # order the build (now pending)
                worker.build(self.unit_type, position, queue=True)
                # remove it from planned
                self.planned_positions_workers.pop(position)
                # track the worker for role maintenance
                self.active_builders.add(worker.tag)
                # make sure it doesn't get selected for another position
                self.roles.set_task(UnitTask.Building, worker)

            # pre-move worker and reserve resources
            elif self.priority and not self.has_build_order(worker):
                # calculate earliest can build
                tech_wait_time = self.prerequisite_completion_time()
                d = worker.distance_to(position)
                travel_time: float = d / to_new_ticks(worker.movement_speed)
                cost = self.knowledge.ai.calculate_cost(self.unit_type)
                adjusted_income = self.income_calculator.mineral_income * 0.93  # 14 / 15 = 0.933333
                available_minerals = self.knowledge.ai.minerals if self.override_reserved else self.knowledge.available_minerals
                minerals_to_collect = max(0.0, cost.minerals - available_minerals)
                minerals_wait_time = minerals_to_collect / max(adjusted_income, 0.01)
                available_gas = self.knowledge.ai.vespene if self.override_reserved else self.knowledge.available_gas
                gas_to_collect = max(0.0, cost.vespene - available_gas)
                gas_wait_time = gas_to_collect / max(self.income_calculator.gas_income, 0.01)
                build_wait_time = max(travel_time, tech_wait_time, minerals_wait_time, gas_wait_time)

                self.knowledge.reserve(cost.minerals, cost.vespene, build_wait_time, self.unit_type)

                if build_wait_time < travel_time + 0.1:
                    # assign the worker
                    self.planned_positions_workers[position] = worker.tag
                if self.planned_positions_workers[position] == worker.tag:  # separate condition prevents waffling
                    # track the worker for role maintenance
                    self.active_builders.add(worker.tag)
                    # make sure it doesn't get selected for another position
                    self.roles.set_task(UnitTask.Building, worker)
                    # pre-move
                    worker.move(self.adjust_build_to_move(position))

        # assign workers to each position (tuples: (position, worker_tag))
        # for each position, worker
            # do the normal stuff?
            # if position is invalid, change the position

        # maintain active builder roles
        for builder_tag in self.active_builders:
            self.roles.set_task(UnitTask.Building, self.cache.by_tag(builder_tag))

        return False

    def plan_positions(self):
        """Find valid potential positions and plan them"""
        pending_and_existing_count = self.get_count(self.unit_type, include_pending=True, include_not_ready=True)
        count_to_plan = self.to_count - pending_and_existing_count

        buildings = self.ai.structures

        # establish race-specific criteria
        def zerg_check(pos: Point2) -> bool:
            creep = self.ai.state.creep
            return self.is_on_creep(creep, pos)

        def protoss_check(pos: Point2) -> bool:
            matrix = self.ai.state.psionic_matrix
            pending_pylons = self.cache.own(UnitTypeId.PYLON).not_ready
            return (
                self.unit_type == UnitTypeId.PYLON
                or matrix.covers(pos)
                or (pending_pylons and pos.distance_to_closest(pending_pylons) <= 7)
            )

        def terran_check(pos: Point2) -> bool:
            # If a structure is landing here from AddonSwap() then dont use this location
            reserved_landing_locations: Set[Point2] = set(
                self.building_solver.structure_target_move_location.values())
            # If this location has a techlab or reactor next to it, then don't create a new structure here
            free_addon_locations: Set[Point2] = set(self.building_solver.free_addon_locations)
            return pos not in reserved_landing_locations and pos not in free_addon_locations

        valid_for_my_race: Callable[[Point2], bool] = lambda pos: True
        if self.knowledge.my_race == Race.Zerg:
            valid_for_my_race = zerg_check
        elif self.knowledge.my_race == Race.Protoss:
            valid_for_my_race = protoss_check
        elif self.knowledge.my_race == Race.Terran:
            valid_for_my_race = terran_check

        # find valid positions
        while len(self.planned_positions_workers) < count_to_plan:
            # verify potential positions left
            if not self.potential_positions:
                self.print(f"Can't find free position to build {self.unit_type.name} in!")
                return
            # check the next candidate
            candidate_position = self.potential_positions.pop(0)
            if buildings.closer_than(1, candidate_position) or not valid_for_my_race(candidate_position):
                continue
            # plan the candidate
            self.planned_positions_workers[candidate_position] = None

    def get_potential_positions(self):
        if self.unit_type in {UnitTypeId.PYLON, UnitTypeId.SUPPLYDEPOT}:
            return self.building_solver.buildings2x2
        else:
            return self.building_solver.buildings3x3

    def adjust_build_to_move(self, position: Point2) -> Point2:
        closest_zone: Optional[Point2] = None
        if self.pather:
            zone_index = self.pather.map.get_zone(position)
            if zone_index > 0:
                closest_zone = self.zone_manager.expansion_zones[zone_index - 1].center_location

        if closest_zone is None:
            closest_zone = position.closest(map_to_point2s_center(self.zone_manager.expansion_zones))

        return position.towards(closest_zone, 1)

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
