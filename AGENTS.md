# LEGO Train App Agent Context

This repository is part of the LEGO Train System.

Before making changes here, always read the shared project context in:

* `../lego_project_context/AGENTS.md`
* `../lego_project_context/01_architecture.md`
* `../lego_project_context/02_services.md`
* `../lego_project_context/03_protocols.md`
* `../lego_project_context/07_decisions.md`

Keep these project rules in mind:

* Follow the event-driven architecture through the WebSocket event bus.
* Do not break service boundaries.
* Use `train_id` as the logical train abstraction, not `device_id`.
* BLE provisioning must remain stateless and send the full config each start.
* Keep the existing multiprocessing model, ports, and protocols.
* Do not introduce blocking calls in async code.
* Prefer simple, robust solutions.
