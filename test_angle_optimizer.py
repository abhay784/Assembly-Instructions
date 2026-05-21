#!/usr/bin/env python3
"""
Quick test of angle optimizer heuristics without needing full pipeline.

Run: python test_angle_optimizer.py
"""

from schemas.instruction import Step, PartRef, StepImage
from finetune.angle_optimizer import suggest_angle, batch_suggest_angles, CameraAngle


def create_test_step(step_id: str, heading: str, parts: list[str], body: str = "") -> Step:
    """Helper to create a test step."""
    return Step(
        step_id=step_id,
        section="Test Section",
        step_number=1,
        heading=heading,
        body_text=body or heading,
        parts_referenced=[PartRef(part_id=p, qty=1, source="explicit") for p in parts],
        images=[StepImage(image_id=step_id, kind="renderable_cad")],
    )


def test_fastener_steps():
    """Test: Fastener steps should use isometric standard."""
    step = create_test_step(
        "fastener_1",
        "Install bolt and washer",
        ["fastener_m5", "washer_metric"]
    )
    angle = suggest_angle(step)
    assert angle is not None
    assert angle.azimuth == 45, f"Expected 45°, got {angle.azimuth}°"
    assert angle.elevation == 35, f"Expected 35°, got {angle.elevation}°"
    print("✓ Fastener steps → isometric standard (45°/35°)")


def test_gear_steps():
    """Test: Gear steps should use face-on angle."""
    step = create_test_step(
        "gear_1",
        "Mesh gears",
        ["gear_12_tooth", "gear_16_tooth"]
    )
    angle = suggest_angle(step)
    assert angle is not None
    assert angle.azimuth == 0, f"Expected 0°, got {angle.azimuth}°"
    assert angle.elevation == 45, f"Expected 45°, got {angle.elevation}°"
    print("✓ Gear steps → face-on angle (0°/45°)")


def test_vertical_assembly():
    """Test: Vertical assembly keywords trigger elevated isometric."""
    step = create_test_step(
        "vert_1",
        "Stack vertical frame",
        ["bracket_upper", "bracket_lower"],
        body="Install the vertical assembly structure"
    )
    # Note: current heuristics check heading/body for "vertical" keyword
    angle = suggest_angle(step)
    assert angle is not None
    # Should be elevated isometric if keyword matches
    if "vertical" in step.heading.lower():
        assert angle.elevation == 45, f"Vertical should have 45° elevation, got {angle.elevation}°"
    print("✓ Vertical assembly detection works")


def test_no_parts():
    """Test: Steps with no parts get default angle."""
    step = create_test_step(
        "empty_1",
        "Generic step",
        []
    )
    angle = suggest_angle(step)
    assert angle is not None
    assert angle.azimuth == 45
    assert angle.elevation == 35
    print("✓ Empty steps → default isometric (45°/35°)")


def test_mixed_parts():
    """Test: Mixed part types use rules in priority order."""
    # Fasteners + structural → fastener rule should win
    step = create_test_step(
        "mixed_1",
        "Assemble bracket fastening",
        ["bracket_a", "fastener_m5", "washer"]
    )
    angle = suggest_angle(step)
    assert angle is not None
    # If all parts <= 3 and includes fastener: isometric standard
    assert angle.azimuth == 45
    assert angle.elevation == 35
    print("✓ Mixed parts + fastener rule → isometric standard")


def test_batch_suggest():
    """Test: Batch suggest works for multiple steps."""
    steps = [
        create_test_step("f1", "Install fastener", ["fastener_m5"]),
        create_test_step("g1", "Mesh gears", ["gear_12", "gear_16"]),
        create_test_step("v1", "Stack vertical parts", ["bracket_upper", "bracket_lower"]),
    ]
    angles = batch_suggest_angles(steps)
    assert len(angles) == 3, f"Expected 3 angles, got {len(angles)}"
    assert all(v is not None for v in angles.values())
    print(f"✓ Batch suggest: {len(angles)} steps → {len(angles)} angles")


def test_angle_validity():
    """Test: All suggested angles have valid ranges."""
    test_steps = [
        create_test_step("t1", "Step 1", ["part_a"]),
        create_test_step("t2", "Step 2", ["bolt_m5", "washer"]),
        create_test_step("t3", "Step 3", ["gear_12"]),
        create_test_step("t4", "Step 4", []),
    ]

    for step in test_steps:
        angle = suggest_angle(step)
        assert angle is not None, f"Step {step.step_id} returned None"
        assert 0 <= angle.azimuth <= 360, f"Azimuth out of range: {angle.azimuth}°"
        assert 0 <= angle.elevation <= 90, f"Elevation out of range: {angle.elevation}°"

    print("✓ All angles within valid ranges (0-360° azimuth, 0-90° elevation)")


def main():
    print("Testing angle optimizer heuristics...\n")

    try:
        test_fastener_steps()
        test_gear_steps()
        test_vertical_assembly()
        test_no_parts()
        test_mixed_parts()
        test_batch_suggest()
        test_angle_validity()

        print("\n✅ All tests passed!")
        print("\nAngle optimizer is ready for pipeline integration.")

    except AssertionError as e:
        print(f"\n❌ Test failed: {e}")
        return 1
    except Exception as e:
        print(f"\n❌ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return 1

    return 0


if __name__ == "__main__":
    exit(main())
