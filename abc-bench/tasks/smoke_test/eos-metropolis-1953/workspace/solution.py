"""
Hard-disk Metropolis Monte Carlo simulation for equation of state calculations.

This module implements the classic Monte Carlo algorithm from:
"Equation of State Calculations by Fast Computing Machines"
Metropolis et al. (1953)

Complete the functions below to implement a 2D hard-disk simulation with
periodic boundary conditions.
"""

from __future__ import annotations


def minimum_image_delta(delta: float, box_length: float) -> float:
    """
    Return the nearest periodic-image displacement.

    For periodic boundaries, there are infinite images of each particle.
    This function returns the displacement to the nearest image.

    Args:
        delta: The raw displacement component (can be any value)
        box_length: The size of the periodic box

    Returns:
        The displacement adjusted to the nearest periodic image
        (should be in the range [-box_length/2, box_length/2])

    Example:
        >>> minimum_image_delta(0.0, 10.0)
        0.0
        >>> minimum_image_delta(4.9, 10.0)
        4.9
        >>> minimum_image_delta(5.1, 10.0)
        -4.9
    """
    raise NotImplementedError


def periodic_distance_squared(
    a: tuple[float, float],
    b: tuple[float, float],
    box_length: float,
) -> float:
    """
    Return squared distance between two particles using periodic boundaries.

    Uses the minimum image convention: the distance is computed to the
    nearest periodic image of particle b.

    Args:
        a: Position (x, y) of first particle
        b: Position (x, y) of second particle
        box_length: Size of the periodic square box

    Returns:
        The squared distance between particles using minimum image convention
    """
    raise NotImplementedError


def has_overlap(
    positions: list[tuple[float, float]],
    particle_index: int,
    candidate: tuple[float, float],
    diameter: float,
    box_length: float,
) -> bool:
    """
    Check if a candidate position overlaps with any other particle.

    For hard disks, particles cannot overlap. This function checks if placing
    a particle at the candidate position would violate the hard-disk constraint.

    Args:
        positions: Current positions of all particles
        particle_index: Index of the particle being moved (should be excluded from checks)
        candidate: The proposed new position to check
        diameter: Hard disk diameter
        box_length: Size of the periodic square box

    Returns:
        True if the candidate position overlaps with any other particle,
        False otherwise
    """
    raise NotImplementedError


def attempt_move(
    positions: list[tuple[float, float]],
    particle_index: int,
    displacement: tuple[float, float],
    diameter: float,
    box_length: float,
) -> tuple[list[tuple[float, float]], bool]:
    """
    Attempt one Metropolis hard-particle move.

    This implements the core Metropolis accept/reject step for hard disks:
    1. Compute the candidate position by applying displacement to the selected particle
    2. Wrap the candidate position into the periodic box [0, box_length)
    3. Check for overlaps with other particles
    4. If no overlap, accept the move; otherwise reject it

    Args:
        positions: Current positions of all particles (must not be mutated)
        particle_index: Index of the particle to move
        displacement: Displacement vector (dx, dy) to apply
        diameter: Hard disk diameter
        box_length: Size of the periodic square box

    Returns:
        A tuple of (new_positions, accepted) where:
        - new_positions: New position list (either with the move applied or unchanged)
        - accepted: True if the move was accepted, False if rejected
    """
    raise NotImplementedError


def run_simulation(
    *,
    initial_positions: list[tuple[float, float]],
    diameter: float,
    box_length: float,
    max_displacement: float,
    seed: int,
    steps: int,
) -> dict[str, float]:
    """
    Run the Monte Carlo hard-disk simulation.

    This is the main simulation driver. It performs the requested number of
    Monte Carlo steps and returns statistics about the simulation.

    Algorithm:
    1. Initialize random number generator with the given seed: rng = random.Random(seed)
    2. For each step:
       a. Select a random particle: particle_index = rng.randrange(len(positions))
       b. Generate random displacement: rng.uniform(-max_displacement, max_displacement) for each component
       c. Attempt the move using attempt_move()
       d. Track acceptance statistics

    Args:
        initial_positions: Starting positions of all particles (must not be mutated)
        diameter: Hard disk diameter
        box_length: Size of the periodic square box
        max_displacement: Maximum displacement in each direction for trial moves
        seed: Random seed for reproducibility
        steps: Number of Monte Carlo steps to perform

    Returns:
        A dictionary containing:
        - "attempted_moves": Total number of moves attempted (float)
        - "accepted_moves": Number of moves accepted (float)
        - "acceptance_fraction": Fraction of moves accepted (float)
        - "packing_fraction": Area fraction occupied by particles (float)
        - "mean_squared_displacement": Average squared displacement from initial positions (float)
        - "final_x_sum": Sum of final x-coordinates (float, for verification)
        - "final_y_sum": Sum of final y-coordinates (float, for verification)

    Note:
        Must use random.Random(seed) for deterministic behavior.
        Particle selection uses rng.randrange(len(positions)).
        Displacement components use rng.uniform(-max_displacement, max_displacement).
    """
    raise NotImplementedError
