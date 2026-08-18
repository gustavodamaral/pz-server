# Project Zomboid configuration

`templates/server.ini.template` contains the settings this project owns. The container renders environment values and merges only those keys into `runtime/zomboid/Server/<PZ_SERVER_NAME>.ini` on each start. Unmanaged keys and PZ-generated identity fields such as `ResetID` and `ServerPlayerID` remain intact.

`PauseEmpty=true` is mandatory and intentionally version controlled.

Build 42 generates its current vanilla `*_SandboxVars.lua`, spawn-region, and spawn-point files on first start. This avoids freezing incomplete settings from an older game build. To customize sandbox behavior reproducibly:

1. Stop the server gracefully.
2. Copy the generated runtime Lua file into a reviewed template in this directory.
3. Add an explicit deployment/copy rule rather than editing a save while it is running.
4. Back up the world before changing world-generation or map settings.

Mods remain disabled initially. Future Workshop IDs go in `WORKSHOP_ITEMS`, mod IDs in `MODS`, and map folders in `MAP_NAMES`; each list uses the delimiter expected by PZ (`;` for Workshop/mod IDs). Put mod maps before `Muldraugh, KY` and back up before the first modded start.
