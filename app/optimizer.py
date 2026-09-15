from __future__ import annotations

import math
import random
from dataclasses import dataclass

from .geo import haversine_km
from .schemas import (
    Place,
    RecommendRequest,
    TransportMode,
)


@dataclass(slots=True)
class Individual:
    genes: list[int]

    objectives: tuple[
        float,
        float,
        float,
    ] = (
        float("inf"),
        float("inf"),
        float("inf"),
    )

    total_minutes: int = 0
    total_distance_km: float = 0.0

    rank: int = 0
    crowding: float = 0.0


def _estimate_minutes(
    distance_km: float,
    mode: TransportMode,
) -> int:
    """
    NSGA 탐색 단계에서 사용하는 대략적인 이동시간.

    실제 최종 코스의 이동시간은
    KakaoRouteClient에서 다시 계산합니다.
    """

    if distance_km <= 0:
        return 0

    if mode == TransportMode.walking:
        # 평균 도보 4.5km/h
        return max(
            1,
            math.ceil(
                distance_km / 4.5 * 60
            ),
        )

    if mode == TransportMode.public_transport:
        # 버스 이동 + 정류장 접근/대기시간 근사
        return max(
            1,
            math.ceil(
                distance_km / 18.0 * 60 + 8
            ),
        )

    # 자동차
    return max(
        1,
        math.ceil(
            distance_km / 30.0 * 60
        ),
    )


def optimize_courses(
    places: list[Place],
    request: RecommendRequest,
    population_size: int,
    generations: int,
    max_places: int,
) -> list[Individual]:

    if len(places) < 2:
        return []

    rng = random.Random(
        request.seed
    )

    max_len = min(
        max_places,
        len(places),
        max(
            2,
            request.available_minutes // 35,
        ),
    )

    min_len = 2

    required_indices = [
        index
        for index, place in enumerate(places)
        if any(
            name.lower()
            in place.title.lower()
            for name
            in request.required_place_names
        )
    ]

    def create() -> Individual:
        length = rng.randint(
            min_len,
            max_len,
        )

        chosen = list(
            dict.fromkeys(
                required_indices
            )
        )

        remaining = [
            index
            for index
            in range(len(places))
            if index not in chosen
        ]

        rng.shuffle(
            remaining
        )

        chosen.extend(
            remaining[
                : max(
                    0,
                    length - len(chosen),
                )
            ]
        )

        rng.shuffle(
            chosen
        )

        return evaluate(
            Individual(
                chosen[:max_len]
            ),
            places,
            request,
        )

    population = [
        create()
        for _ in range(
            max(
                10,
                population_size,
            )
        )
    ]

    for _ in range(
        max(
            1,
            generations,
        )
    ):
        fronts = non_dominated_sort(
            population
        )

        for front in fronts:
            assign_crowding(
                front
            )

        offspring: list[
            Individual
        ] = []

        while len(offspring) < population_size:
            p1 = tournament(
                population,
                rng,
            )

            p2 = tournament(
                population,
                rng,
            )

            c1, c2 = crossover(
                p1,
                p2,
                len(places),
                max_len,
                rng,
            )

            offspring.extend(
                [
                    mutate(
                        c1,
                        len(places),
                        min_len,
                        max_len,
                        rng,
                    ),
                    mutate(
                        c2,
                        len(places),
                        min_len,
                        max_len,
                        rng,
                    ),
                ]
            )

        combined = [
            evaluate(
                individual,
                places,
                request,
            )
            for individual
            in (
                population
                + offspring[:population_size]
            )
        ]

        new_population: list[
            Individual
        ] = []

        for front in non_dominated_sort(
            combined
        ):
            assign_crowding(
                front
            )

            if (
                len(new_population)
                + len(front)
                <= population_size
            ):
                new_population.extend(
                    front
                )

            else:
                front.sort(
                    key=lambda item:
                    item.crowding,
                    reverse=True,
                )

                new_population.extend(
                    front[
                        :
                        population_size
                        - len(new_population)
                    ]
                )

                break

        population = new_population

    fronts = non_dominated_sort(
        population
    )

    pareto = (
        fronts[0]
        if fronts
        else population
    )

    unique: dict[
        tuple[int, ...],
        Individual,
    ] = {}

    for individual in pareto:
        unique.setdefault(
            tuple(
                individual.genes
            ),
            individual,
        )

    candidates = list(
        unique.values()
    )

    if not candidates:
        return []

    # ------------------------------------------------
    # 반환 순서를 반드시 고정
    #
    # 0 → 이동 최소형
    # 1 → 혼잡 회피형
    # 2 → 선호 충족형
    # ------------------------------------------------

    selected: list[
        Individual
    ] = []

    used: set[
        tuple[int, ...]
    ] = set()

    for objective_index in range(3):

        ordered = sorted(
            candidates,
            key=lambda item: (
                item.objectives[
                    objective_index
                ],
                sum(
                    item.objectives
                ),
            ),
        )

        chosen = next(
            (
                item
                for item in ordered
                if tuple(
                    item.genes
                )
                not in used
            ),
            ordered[0],
        )

        selected.append(
            chosen
        )

        used.add(
            tuple(
                chosen.genes
            )
        )

    return selected


def evaluate(
    ind: Individual,
    places: list[Place],
    request: RecommendRequest,
) -> Individual:

    route = [
        places[index]
        for index
        in ind.genes
    ]

    current_lat = request.latitude
    current_lon = request.longitude

    distance = 0.0
    travel_minutes = 0

    congestion = 0.0
    mismatch = 0.0

    invalid_penalty = 0.0

    category_run = 1
    previous_category: str | None = None

    for place in route:

        leg = haversine_km(
            current_lat,
            current_lon,
            place.latitude,
            place.longitude,
        )

        distance += leg

        travel_minutes += _estimate_minutes(
            leg,
            request.transport,
        )

        current_lat = place.latitude
        current_lon = place.longitude

        # -----------------------------------------
        # 혼잡 회피 목적함수
        # -----------------------------------------

        congestion += (
            place.congestion_score
            if place.congestion_score is not None
            else 50.0
        )

        # -----------------------------------------
        # 사용자 선호 목적함수
        #
        # services.py에서 계산된
        # preference_score를 사용
        # -----------------------------------------

        if request.preferences:

            preference = (
                place.preference_score
                if place.preference_score
                is not None
                else 0.0
            )

            preference = max(
                0.0,
                min(
                    1.0,
                    preference,
                ),
            )

            mismatch += (
                1.0 - preference
            )

        # 같은 종류 장소가 3개 이상
        # 연속되는 코스 억제

        if (
            previous_category
            == place.category
        ):
            category_run += 1

            if category_run >= 3:
                invalid_penalty += 30.0

        else:
            category_run = 1

        previous_category = (
            place.category
        )

    # -----------------------------------------
    # 필수 관광지 누락 패널티
    # -----------------------------------------

    route_titles = [
        place.title.lower()
        for place in route
    ]

    for required in (
        request.required_place_names
    ):
        if not any(
            required.lower()
            in title
            for title
            in route_titles
        ):
            invalid_penalty += 100.0

    # -----------------------------------------
    # 체류시간
    # -----------------------------------------

    stay = sum(
        max(
            20,
            request.available_minutes
            // max(
                len(route) * 2,
                1,
            ),
        )
        for _ in route
    )

    total = (
        travel_minutes
        + stay
    )

    if (
        total
        > request.available_minutes
    ):
        invalid_penalty += (
            total
            - request.available_minutes
        ) * 5.0

    # 도보 2km 초과인데
    # 휴식 포인트가 없으면 패널티

    if (
        request.transport
        == TransportMode.walking
        and distance > 2.0
        and not any(
            place.is_rest_point
            for place in route
        )
    ):
        invalid_penalty += 10.0

    ind.total_minutes = total
    ind.total_distance_km = distance

    ind.objectives = (
        # 이동 최소형
        travel_minutes
        + invalid_penalty,

        # 혼잡 회피형
        congestion
        + invalid_penalty,

        # 선호 충족형
        mismatch
        + invalid_penalty,
    )

    return ind


def dominates(
    a: Individual,
    b: Individual,
) -> bool:

    return (
        all(
            x <= y
            for x, y
            in zip(
                a.objectives,
                b.objectives,
                strict=False,
            )
        )
        and any(
            x < y
            for x, y
            in zip(
                a.objectives,
                b.objectives,
                strict=False,
            )
        )
    )


def non_dominated_sort(
    population: list[Individual],
) -> list[list[Individual]]:

    dominated_count = {
        id(item): 0
        for item in population
    }

    dominates_set: dict[
        int,
        list[Individual],
    ] = {
        id(item): []
        for item in population
    }

    fronts: list[
        list[Individual]
    ] = [[]]

    for p in population:

        for q in population:

            if p is q:
                continue

            if dominates(
                p,
                q,
            ):
                dominates_set[
                    id(p)
                ].append(
                    q
                )

            elif dominates(
                q,
                p,
            ):
                dominated_count[
                    id(p)
                ] += 1

        if (
            dominated_count[
                id(p)
            ]
            == 0
        ):
            p.rank = 0

            fronts[0].append(
                p
            )

    index = 0

    while (
        index < len(fronts)
        and fronts[index]
    ):
        next_front: list[
            Individual
        ] = []

        for p in fronts[index]:

            for q in dominates_set[
                id(p)
            ]:
                dominated_count[
                    id(q)
                ] -= 1

                if (
                    dominated_count[
                        id(q)
                    ]
                    == 0
                ):
                    q.rank = (
                        index + 1
                    )

                    next_front.append(
                        q
                    )

        if next_front:
            fronts.append(
                next_front
            )

        index += 1

    return fronts


def assign_crowding(
    front: list[Individual],
) -> None:

    if not front:
        return

    for item in front:
        item.crowding = 0.0

    for objective in range(3):

        front.sort(
            key=lambda item:
            item.objectives[
                objective
            ]
        )

        front[0].crowding = float(
            "inf"
        )

        front[-1].crowding = float(
            "inf"
        )

        min_v = front[
            0
        ].objectives[
            objective
        ]

        max_v = front[
            -1
        ].objectives[
            objective
        ]

        if max_v == min_v:
            continue

        for index in range(
            1,
            len(front) - 1,
        ):
            front[
                index
            ].crowding += (
                front[
                    index + 1
                ].objectives[
                    objective
                ]
                - front[
                    index - 1
                ].objectives[
                    objective
                ]
            ) / (
                max_v
                - min_v
            )


def tournament(
    population: list[Individual],
    rng: random.Random,
) -> Individual:

    a, b = rng.sample(
        population,
        2,
    )

    if a.rank != b.rank:

        return (
            a
            if a.rank < b.rank
            else b
        )

    return (
        a
        if a.crowding
        >= b.crowding
        else b
    )


def crossover(
    a: Individual,
    b: Individual,
    universe: int,
    max_len: int,
    rng: random.Random,
) -> tuple[
    Individual,
    Individual,
]:

    def make(
        first: list[int],
        second: list[int],
    ) -> Individual:

        cut = rng.randint(
            1,
            max(
                1,
                len(first),
            ),
        )

        genes = (
            first[:cut]
            + [
                value
                for value
                in second
                if value
                not in first[:cut]
            ]
        )

        if len(genes) < 2:

            extras = [
                value
                for value
                in range(universe)
                if value
                not in genes
            ]

            rng.shuffle(
                extras
            )

            genes.extend(
                extras[
                    :
                    2 - len(genes)
                ]
            )

        return Individual(
            genes[:max_len]
        )

    return (
        make(
            a.genes,
            b.genes,
        ),
        make(
            b.genes,
            a.genes,
        ),
    )


def mutate(
    ind: Individual,
    universe: int,
    min_len: int,
    max_len: int,
    rng: random.Random,
) -> Individual:

    genes = ind.genes[:]

    if (
        len(genes) >= 2
        and rng.random() < 0.5
    ):
        i, j = rng.sample(
            range(
                len(genes)
            ),
            2,
        )

        genes[i], genes[j] = (
            genes[j],
            genes[i],
        )

    if rng.random() < 0.35:

        available = [
            value
            for value
            in range(universe)
            if value
            not in genes
        ]

        if (
            available
            and len(genes)
            < max_len
        ):
            genes.insert(
                rng.randrange(
                    len(genes) + 1
                ),
                rng.choice(
                    available
                ),
            )

        elif len(genes) > min_len:
            genes.pop(
                rng.randrange(
                    len(genes)
                )
            )

    return Individual(
        genes
    )