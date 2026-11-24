--[==[
DynaCamp <-> DCS bridge (mission scripting environment, Lua 5.1).

Connects to the DynaCamp engine over TCP (JSON lines) and:
  * reports human players (position, coalition, category) -> player bubbles
  * spawns / routes / despawns the groups the engine materialises
  * reports alive units and positions of those groups

It is deliberately dumb: the engine builds complete group tables, this script
only executes them. Requires LuaSocket, i.e. a *desanitised* MissionScripting.lua
(comment out the sanitizeModule('require') / sanitizeModule('package') lines).

Load it from a mission trigger (DO SCRIPT):
    dofile(lfs.writedir() .. [[Scripts\dynacamp_bridge.lua]])
Optional overrides, set before loading:
    DYNACAMP_HOST = "127.0.0.1"; DYNACAMP_PORT = 50555
]==]

DynaCamp = DynaCamp or {}
local DC = DynaCamp

DC.host = DYNACAMP_HOST or "127.0.0.1"
DC.port = DYNACAMP_PORT or 50555
DC.tick = 0.2           -- seconds between network polls
DC.report_every = 2.0   -- seconds between player/state reports
DC.retry_every = 5.0    -- seconds between reconnection attempts
DC.entities = DC.entities or {} -- entity id -> { groups = {name, ...} }
DC.client = nil
DC.rxbuf = ""
DC.last_report = 0
DC.last_retry = -1e9

local function log(msg)
  if env and env.info then env.info("[DynaCamp] " .. tostring(msg)) else print("[DynaCamp] " .. tostring(msg)) end
end
DC.log = log

---------------------------------------------------------------- JSON
local json = {}
DC.json = json

local escapes = { ['"'] = '\\"', ['\\'] = '\\\\', ['\b'] = '\\b', ['\f'] = '\\f',
                  ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }

local function is_array(t)
  if next(t) == nil then return true end
  local n = #t
  if n == 0 then return false end
  for k in pairs(t) do
    if type(k) ~= "number" or k < 1 or k > n or k % 1 ~= 0 then return false end
  end
  return true
end

function json.encode(v)
  local ty = type(v)
  if v == nil then return "null"
  elseif ty == "boolean" then return v and "true" or "false"
  elseif ty == "number" then
    if v ~= v or v == math.huge or v == -math.huge then return "null" end
    if v % 1 == 0 and math.abs(v) < 1e15 then return string.format("%d", v) end
    return string.format("%.14g", v)
  elseif ty == "string" then
    return '"' .. v:gsub('[%c"\\]', function(c)
      return escapes[c] or string.format("\\u%04x", c:byte())
    end) .. '"'
  elseif ty == "table" then
    local out = {}
    if is_array(v) then
      for i = 1, #v do out[i] = json.encode(v[i]) end
      return "[" .. table.concat(out, ",") .. "]"
    end
    for k, val in pairs(v) do
      out[#out + 1] = json.encode(tostring(k)) .. ":" .. json.encode(val)
    end
    return "{" .. table.concat(out, ",") .. "}"
  end
  error("cannot encode " .. ty)
end

local function skip_ws(s, i)
  local _, e = s:find("^[ \n\r\t]*", i)
  return e + 1
end

local decode_value

local function decode_string(s, i)
  -- s:sub(i, i) == '"'
  local out, j = {}, i + 1
  while true do
    local c = s:sub(j, j)
    if c == "" then error("unterminated string") end
    if c == '"' then return table.concat(out), j + 1 end
    if c == "\\" then
      local n = s:sub(j + 1, j + 1)
      local map = { b = "\b", f = "\f", n = "\n", r = "\r", t = "\t", ['"'] = '"', ["\\"] = "\\", ["/"] = "/" }
      if map[n] then out[#out + 1] = map[n]; j = j + 2
      elseif n == "u" then
        local code = tonumber(s:sub(j + 2, j + 5), 16)
        if code < 128 then out[#out + 1] = string.char(code)
        elseif code < 2048 then
          out[#out + 1] = string.char(192 + math.floor(code / 64), 128 + code % 64)
        else
          out[#out + 1] = string.char(224 + math.floor(code / 4096), 128 + math.floor(code / 64) % 64, 128 + code % 64)
        end
        j = j + 6
      else error("bad escape at " .. j) end
    else
      out[#out + 1] = c; j = j + 1
    end
  end
end

decode_value = function(s, i)
  i = skip_ws(s, i)
  local c = s:sub(i, i)
  if c == "{" then
    local obj = {}
    i = skip_ws(s, i + 1)
    if s:sub(i, i) == "}" then return obj, i + 1 end
    while true do
      local key
      key, i = decode_string(s, skip_ws(s, i))
      i = skip_ws(s, i)
      if s:sub(i, i) ~= ":" then error("expected ':' at " .. i) end
      obj[key], i = decode_value(s, i + 1)
      i = skip_ws(s, i)
      local d = s:sub(i, i)
      if d == "}" then return obj, i + 1 end
      if d ~= "," then error("expected ',' or '}' at " .. i) end
      i = i + 1
    end
  elseif c == "[" then
    local arr, n = {}, 0
    i = skip_ws(s, i + 1)
    if s:sub(i, i) == "]" then return arr, i + 1 end
    while true do
      n = n + 1
      arr[n], i = decode_value(s, i)
      i = skip_ws(s, i)
      local d = s:sub(i, i)
      if d == "]" then return arr, i + 1 end
      if d ~= "," then error("expected ',' or ']' at " .. i) end
      i = i + 1
    end
  elseif c == '"' then return decode_string(s, i)
  elseif s:sub(i, i + 3) == "true" then return true, i + 4
  elseif s:sub(i, i + 4) == "false" then return false, i + 5
  elseif s:sub(i, i + 3) == "null" then return nil, i + 4
  else
    local num = s:match("^-?%d+%.?%d*[eE]?[-+]?%d*", i)
    if not num or num == "" then error("unexpected '" .. c .. "' at " .. i) end
    return tonumber(num), i + #num
  end
end

function json.decode(s)
  local v = decode_value(s, 1)
  return v
end

---------------------------------------------------------------- network
function DC.connect()
  if not DC.socket then
    package.path = package.path .. ";.\\LuaSocket\\?.lua"
    package.cpath = package.cpath .. ";.\\LuaSocket\\?.dll"
    DC.socket = require("socket")
  end
  local client, err = DC.socket.connect(DC.host, DC.port)
  if not client then
    return false, err
  end
  client:settimeout(0)
  client:setoption("tcp-nodelay", true)
  DC.client = client
  DC.rxbuf = ""
  log("connected to engine at " .. DC.host .. ":" .. DC.port)
  local theatre = env and env.mission and env.mission.theatre or "unknown"
  DC.send({ msg = "hello", theatre = theatre, time = timer.getTime() })
  return true
end

function DC.disconnect(reason)
  if DC.client then pcall(function() DC.client:close() end) end
  DC.client = nil
  log("disconnected: " .. tostring(reason))
  -- The engine takes everything back when the link drops: remove our groups.
  for id, _ in pairs(DC.entities) do DC.destroy_entity(id) end
end

function DC.send(tbl)
  if not DC.client then return false end
  local ok, err = DC.client:send(json.encode(tbl) .. "\n")
  if not ok and err == "closed" then DC.disconnect("send: closed"); return false end
  return true
end

function DC.receive()
  while DC.client do
    local line, err, partial = DC.client:receive("*l", DC.rxbuf)
    if line then
      DC.rxbuf = ""
      if #line > 0 then
        local ok, msg = pcall(json.decode, line)
        if ok and type(msg) == "table" then
          local hok, herr = pcall(DC.handle, msg)
          if not hok then log("handler error: " .. tostring(herr)) end
        else
          log("bad json: " .. tostring(msg))
        end
      end
    elseif err == "timeout" then
      DC.rxbuf = partial or ""
      return
    else
      DC.disconnect("receive: " .. tostring(err))
      return
    end
  end
end

---------------------------------------------------------------- commands
local handlers = {}
DC.handlers = handlers

function DC.handle(msg)
  local h = handlers[msg.cmd]
  if h then h(msg) else log("unknown command " .. tostring(msg.cmd)) end
end

-- Group.Category values (fallbacks if the enum is missing)
local CATEGORY = { airplane = 0, helicopter = 1, ground = 2, ship = 3 }
local CATEGORY_KEY = { airplane = "AIRPLANE", helicopter = "HELICOPTER", ground = "GROUND", ship = "SHIP" }

local function group_category(name)
  local key = CATEGORY_KEY[name or "ground"] or "GROUND"
  if Group and Group.Category and Group.Category[key] then return Group.Category[key] end
  return CATEGORY[name or "ground"] or CATEGORY.ground
end

local function deepcopy(v)
  if type(v) ~= "table" then return v end
  local out = {}
  for k, x in pairs(v) do out[k] = deepcopy(x) end
  return out
end
DC.deepcopy = deepcopy

---------------------------------------------------------------- templates
-- Late-activated mission-editor groups named "DC_TPL_<aircraft>[_<TASK>]"
-- (e.g. DC_TPL_f16c_SEAD) give aircraft type, livery and payload to spawns.
DC.templates = nil

function DC.index_templates()
  DC.templates = {}
  local mission = env and env.mission
  if not (mission and mission.coalition) then return end
  for _, side in pairs(mission.coalition) do
    for _, country in ipairs(side.country or {}) do
      for _, catname in ipairs({ "plane", "helicopter" }) do
        local cat = country[catname]
        for _, grp in ipairs((cat and cat.group) or {}) do
          if type(grp.name) == "string" and grp.name:find("^DC_TPL") then
            DC.templates[grp.name] = grp
          end
        end
      end
    end
  end
end

function DC.apply_template(g)
  if not g.templates then return end
  if not DC.templates then DC.index_templates() end
  for _, tname in ipairs(g.templates) do
    local tpl = DC.templates[tname]
    if tpl and tpl.units and #tpl.units > 0 then
      for i, u in ipairs(g.data.units) do
        local tu = tpl.units[math.min(i, #tpl.units)]
        u.type = tu.type
        u.payload = deepcopy(tu.payload)
        u.livery_id = tu.livery_id
        u.callsign = u.callsign or deepcopy(tu.callsign)  -- the ATO's callsign wins
        u.onboard_num = tu.onboard_num
      end
      -- the ATO's frequency wins: it is clear of the airfields' ATC
      g.data.frequency = g.data.frequency or tpl.frequency
      return tname
    end
  end
  log("no template for " .. tostring(g.data.name) .. ": spawning without payload")
end

--[==[ Western callsigns arrive as { index = { list index, flight, unit },
name = "Lobo31" } (JSON has no numeric keys): DCS wants { [1], [2], [3], name }. ]==]
function DC.apply_callsign(g)
  for _, u in ipairs((g.data and g.data.units) or {}) do
    local c = u.callsign
    if type(c) == "table" and c.index then
      u.callsign = { c.index[1], c.index[2], c.index[3], name = c.name }
    end
  end
end

function DC.apply_landing(g)
  if not g.land_at or not Airbase then return end
  local ab = Airbase.getByName(g.land_at)
  local points = g.data.route and g.data.route.points
  if ab and points and #points > 0 then
    local last = points[#points]
    last.type = "Land"
    last.action = "Landing"
    last.airdromeId = ab:getID()
  end
end

-- Put ground vehicles on the nearest road if it is close: the engine already lines
-- them up on (OpenStreetMap) roads; this absorbs the small differences with the
-- game's road network so nothing ends up stuck in a forest.
DC.road_snap_m = 300
function DC.snap_to_road(g)
  if not g.snap_to_road or not land or not land.getClosestPointOnRoads then return end
  for _, u in ipairs(g.data.units) do
    local ok, rx, ry = pcall(land.getClosestPointOnRoads, "roads", u.x, u.y)
    if ok and rx and ry then
      local dx, dy = rx - u.x, ry - u.y
      if dx * dx + dy * dy <= DC.road_snap_m * DC.road_snap_m then u.x, u.y = rx, ry end
    end
  end
  g.data.x, g.data.y = g.data.units[1].x, g.data.units[1].y
end

-- Air defence: radars on and weapons free as soon as the site exists.
function DC.alarm_red(name)
  if not (AI and AI.Option and AI.Option.Ground) then return end
  local grp = Group.getByName(name)
  if not grp then return end
  local ctl = grp:getController()
  pcall(ctl.setOption, ctl, AI.Option.Ground.id.ALARM_STATE, AI.Option.Ground.val.ALARM_STATE.RED)
  pcall(ctl.setOption, ctl, AI.Option.Ground.id.ROE, AI.Option.Ground.val.ROE.OPEN_FIRE)
end

function handlers.spawn(msg)
  local names = {}
  local ok, err = pcall(function()
    for _, g in ipairs(msg.groups) do
      DC.snap_to_road(g)
      DC.apply_callsign(g)
      DC.apply_template(g)
      DC.apply_landing(g)
      coalition.addGroup(g.country, group_category(g.category), g.data)
      names[#names + 1] = g.data.name
      if g.alarm_red then DC.alarm_red(g.data.name) end
    end
  end)
  DC.entities[msg.id] = { groups = names }
  if not ok then
    DC.destroy_entity(msg.id)
    DC.send({ msg = "spawned", id = msg.id, ok = false, error = tostring(err) })
  else
    DC.send({ msg = "spawned", id = msg.id, ok = true })
  end
end

function handlers.despawn(msg)
  local units = DC.collect_units(msg.id)
  DC.destroy_entity(msg.id)
  DC.send({ msg = "despawned", id = msg.id, units = units })
end

function handlers.route(msg)
  local ent = DC.entities[msg.id]
  if not ent then return end
  for _, name in ipairs(ent.groups) do
    local grp = Group.getByName(name)
    if grp and grp:isExist() then
      grp:getController():setTask({ id = "Mission", params = { route = { points = msg.points } } })
    end
  end
end

---------------------------------------------------------------- ATO menus
-- The engine publishes, per coalition, the packages players can join. Every
-- player group gets an F10 menu "DynaCamp ATO" rebuilt when the board changes.
DC.ato = DC.ato or {}       -- coalition -> { version = n, packages = {...} }
DC.players = DC.players or {} -- player name -> { gid, coalition, unit, type, category }
DC.menus = DC.menus or {}     -- gid -> { version = n, path = menu path, player = name }
DC.marks = DC.marks or {}     -- player name -> { mark ids }
DC.next_mark = DC.next_mark or 72000

function handlers.ato(msg)
  DC.ato[msg.coalition] = { version = msg.version, packages = msg.packages or {} }
  DC.refresh_menus()
end

local function request(args)
  DC.send(args)
end

function DC.build_menu(gid, info, player)
  local old = DC.menus[gid]
  if old and old.path then pcall(missionCommands.removeItemForGroup, gid, old.path) end
  local board = DC.ato[info.coalition] or { version = 0, packages = {} }
  local root = missionCommands.addSubMenuForGroup(gid, "DynaCamp ATO")
  missionCommands.addCommandForGroup(gid, "My assignment", root, request, { msg = "briefing", player = player })
  missionCommands.addCommandForGroup(gid, "Leave my flight", root, request, { msg = "leave", player = player })
  for i, pkg in ipairs(board.packages) do
    if i > 8 then break end
    local sub = missionCommands.addSubMenuForGroup(gid, pkg.label, root)
    for j, fl in ipairs(pkg.flights or {}) do
      if j > 9 then break end
      missionCommands.addCommandForGroup(gid, "Join " .. fl.label, sub, request,
        { msg = "join", player = player, flight = fl.id, type = info.type, category = info.category })
    end
  end
  DC.menus[gid] = { version = board.version, path = root, player = player }
end

function DC.refresh_menus()
  if not missionCommands then return end
  for player, info in pairs(DC.players) do
    local board = DC.ato[info.coalition]
    local menu = DC.menus[info.gid]
    local version = board and board.version or 0
    if not menu or menu.version ~= version or menu.player ~= player then
      local ok, err = pcall(DC.build_menu, info.gid, info, player)
      if not ok then log("menu error: " .. tostring(err)) end
    end
  end
end

function handlers.notify(msg)
  local info = DC.players[msg.player]
  if not info then return end
  trigger.action.outTextForGroup(info.gid, msg.text, msg.duration or 30)
  for _, id in ipairs(DC.marks[msg.player] or {}) do pcall(trigger.action.removeMark, id) end
  local ids = {}
  for _, m in ipairs(msg.marks or {}) do
    DC.next_mark = DC.next_mark + 1
    trigger.action.markToGroup(DC.next_mark, m.text, { x = m.x, y = 0, z = m.z }, info.gid, true)
    ids[#ids + 1] = DC.next_mark
  end
  DC.marks[msg.player] = ids
end

---------------------------------------------------------------- player events
local PLAYER_EVENTS = {}
if world and world.event then
  PLAYER_EVENTS[world.event.S_EVENT_TAKEOFF or -1] = "takeoff"
  PLAYER_EVENTS[world.event.S_EVENT_LAND or -1] = "landed"
  PLAYER_EVENTS[world.event.S_EVENT_CRASH or -1] = "lost"
  PLAYER_EVENTS[world.event.S_EVENT_EJECTION or -1] = "lost"
  PLAYER_EVENTS[world.event.S_EVENT_PILOT_DEAD or -1] = "lost"
end
DC.PLAYER_EVENTS = PLAYER_EVENTS
DC.lost_reported = DC.lost_reported or {}

DC.event_handler = {}
function DC.event_handler:onEvent(e)
  local kind = PLAYER_EVENTS[e.id]
  if not kind or not e.initiator then return end
  local ok, player = pcall(function() return e.initiator:getPlayerName() end)
  if not ok or not player then return end
  if kind == "lost" then
    if DC.lost_reported[player] then return end -- crash + eject + dead: report once
    DC.lost_reported[player] = true
  elseif kind == "takeoff" then
    DC.lost_reported[player] = nil
  end
  DC.send({ msg = "player_event", player = player, event = kind })
end

function handlers.ping(msg)
  DC.send({ msg = "pong", time = timer.getTime() })
end

function DC.destroy_entity(id)
  local ent = DC.entities[id]
  if not ent then return end
  for _, name in ipairs(ent.groups) do
    local grp = Group.getByName(name)
    if grp and grp:isExist() then grp:destroy() end
  end
  DC.entities[id] = nil
end

---------------------------------------------------------------- reports
function DC.collect_units(id)
  local out = {}
  local ent = DC.entities[id]
  if not ent then return out end
  for _, name in ipairs(ent.groups) do
    local grp = Group.getByName(name)
    if grp and grp:isExist() then
      for _, u in ipairs(grp:getUnits()) do
        if u:isExist() and u:getLife() >= 1 then
          local p = u:getPoint()
          out[#out + 1] = { n = u:getName(), x = p.x, z = p.z }
        end
      end
    end
  end
  return out
end

function DC.report_players()
  local players, seen = {}, {}
  for _, side in ipairs({ 1, 2 }) do
    for _, u in ipairs(coalition.getPlayers(side) or {}) do
      if u and u:isExist() then
        local p = u:getPoint()
        local desc = u:getDesc() or {}
        local name = u:getPlayerName() or u:getName()
        local gid = u:getGroup():getID()
        local typ = u:getTypeName()
        seen[name] = { gid = gid, coalition = side, unit = u:getName(), type = typ, category = desc.category or 0 }
        players[#players + 1] = { id = name, name = name, coalition = side, type = typ,
                                  category = desc.category or 0, x = p.x, z = p.z, alt = p.y }
      end
    end
  end
  DC.players = seen
  DC.send({ msg = "players", players = players })
  DC.refresh_menus()
end

function DC.report_state()
  local entities = {}
  for id, _ in pairs(DC.entities) do
    entities[#entities + 1] = { id = id, units = DC.collect_units(id) }
  end
  if #entities > 0 then DC.send({ msg = "state", entities = entities }) end
end

---------------------------------------------------------------- main loop
function DC.step(now)
  if not DC.client then
    if now - DC.last_retry >= DC.retry_every then
      DC.last_retry = now
      local ok, err = pcall(DC.connect)
      if not ok then log("connect error: " .. tostring(err)) end
    end
    return
  end
  DC.receive()
  if DC.client and now - DC.last_report >= DC.report_every then
    DC.last_report = now
    DC.report_players()
    DC.report_state()
  end
end

function DC.loop(_, now)
  local ok, err = pcall(DC.step, now)
  if not ok then log("step error: " .. tostring(err)) end
  return now + DC.tick
end

if not DYNACAMP_NO_AUTOSTART then
  if world and world.addEventHandler then world.addEventHandler(DC.event_handler) end
  timer.scheduleFunction(DC.loop, nil, timer.getTime() + 1)
  log("bridge loaded, engine " .. DC.host .. ":" .. DC.port)
end

return DC
