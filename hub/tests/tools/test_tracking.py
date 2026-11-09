import asyncio

from homehub import household
from homehub.tools import CallContext, current_context, result_texts
from homehub.tools.tracking import build_module


def call(module, name, args, person_id):
    handler = next(spec.fn.handler for spec in module.tools if spec.name == name)
    token = current_context.set(CallContext(room="bedroom", person_id=person_id))
    try:
        return result_texts(asyncio.run(handler(args)))[0]
    finally:
        current_context.reset(token)


def test_packages_answer_from_the_persons_own_list(private_config, temp_storage):
    household.sync_household(private_config, temp_storage, household.EXAMPLE_PATH)
    module = build_module(private_config, temp_storage)
    assert call(module, "packages", {}, "alex").startswith("No packages on the way")
    assert call(module, "track_package", {"number": "1Z999AA10123456784", "what": "plant pot"}, "alex").startswith("Added 1Z999AA10123456784 (UPS)")
    assert "plant pot: tracking by UPS [tracking 1Z999AA10123456784]" in call(module, "packages", {}, "alex")
    assert call(module, "packages", {}, "sam").startswith("No packages on the way")
    assert "doesn't look like a tracking number" in call(module, "track_package", {"number": "nope"}, "alex")
    call(module, "track_package", {"number": "9400 1000 0000 0000 0000 00", "what": "birthday gift"}, "alex")
    assert call(module, "remove_package", {"which": "lamp"}, "alex") == "No package matches that. On the list: birthday gift; plant pot."
    assert call(module, "remove_package", {"which": "plant"}, "sam").startswith("No package matches that")
    assert call(module, "remove_package", {"which": "PLANT pot"}, "alex") == "Took plant pot off the package list."
    assert "plant pot" not in call(module, "packages", {}, "alex") and "birthday gift" in call(module, "packages", {}, "alex")
    assert "Gmail isn't connected" in call(module, "check_packages", {}, "alex")
