-- Runs the DCS bridge against mocked DCS and LuaSocket APIs (lua5.1, like DCS).
-- Usage: lua5.1 tests/lua/test_bridge.lua path/to/dynacamp_bridge.lua

local bridge_path = arg[1]
local failures = 0
local function check(cond, what)
  if not cond then failures = failures + 1; print("FAIL: " .. what) else print("ok   " .. what) end
end

---------------------------------------------------------------- DCS mocks
local now = 0
timer = { getTime = function() return now end, scheduleFunction = function() end }
env = { info = function() end, mission = { theatre = "Caucasus", coalition = {
  blue = { country = { { plane = { group = {
    { name = "DC_TPL_f16c_SEAD", units = { { type = "F-16C_50", payload = { pylons = { [1] = { CLSID = "HARM" } } }, livery_id = "viper" } } },
    { name = "Some other group", units = { { type = "F-16C_50" } } },
  } } } } } } } }

world = { event = { S_EVENT_TAKEOFF = 3, S_EVENT_LAND = 4, S_EVENT_CRASH = 5, S_EVENT_EJECTION = 6, S_EVENT_PILOT_DEAD = 9 },
          addEventHandler = function() end }
Airbase = { getByName = function(name) if name == "Kutaisi" then return { getID = function() return 26 end } end end }

local menus, texts, marks = {}, {}, {}
missionCommands = {}
function missionCommands.addSubMenuForGroup(gid, label, parent)
  local path = { parent = parent, label = label }
  menus[#menus + 1] = { gid = gid, label = label, parent = parent, path = path }
  return path
end
function missionCommands.addCommandForGroup(gid, label, parent, fn, args)
  menus[#menus + 1] = { gid = gid, label = label, parent = parent, fn = fn, args = args }
end
function missionCommands.removeItemForGroup(gid, path)
  local keep = {}
  for _, m in ipairs(menus) do
    local p, inside = m.parent, (m.path == path)
    while p and not inside do inside = (p == path); p = p.parent end
    if not inside then keep[#keep + 1] = m end
  end
  menus = keep
end
trigger = { action = {
  outTextForGroup = function(gid, text) texts[#texts + 1] = { gid = gid, text = text } end,
  markToGroup = function(id, text, pos, gid) marks[id] = { text = text, pos = pos, gid = gid } end,
  removeMark = function(id) marks[id] = nil end,
} }

local groups = {}
local function make_unit(name, x, z)
  local u = { name = name, x = x, z = z, life = 10, alive = true }
  function u:isExist() return self.alive end
  function u:getLife() return self.life end
  function u:getPoint() return { x = self.x, y = 0, z = self.z } end
  function u:getName() return self.name end
  return u
end

Group = { Category = { GROUND = 2, SHIP = 3 } }
function Group.getByName(name) return groups[name] end

local players_by_side = { [1] = {}, [2] = {} }
coalition = {}
function coalition.addGroup(country, category, data)
  if data.units[1].type == "BOOM" then error("unknown unit type") end
  local g = { name = data.name, units = {}, alive = true, tasks = {}, data = data, category = category }
  for _, u in ipairs(data.units) do g.units[#g.units + 1] = make_unit(u.name, u.x, u.y) end
  function g:isExist() return self.alive end
  function g:getUnits() return self.units end
  function g:destroy() self.alive = false; groups[self.name] = nil end
  function g:getController()
    return { setTask = function(_, task) g.tasks[#g.tasks + 1] = task end }
  end
  groups[data.name] = g
  return g
end
function coalition.getPlayers(side) return players_by_side[side] end

---------------------------------------------------------------- socket mock
local sent, inbound = {}, {}
local fake_client = {}
function fake_client:settimeout() end
function fake_client:setoption() end
function fake_client:close() end
function fake_client:send(data) sent[#sent + 1] = data; return #data end
function fake_client:receive(pattern, prefix)
  local chunk = table.remove(inbound, 1)
  if chunk == nil then return nil, "timeout", "" end
  if chunk.partial then return nil, "timeout", (prefix or "") .. chunk.partial end
  return (prefix or "") .. chunk.line
end
package.loaded["socket"] = { connect = function() return fake_client end }

---------------------------------------------------------------- load bridge
DYNACAMP_NO_AUTOSTART = true
local DC = dofile(bridge_path)
local json = DC.json

local function last_sent()
  return json.decode((sent[#sent]:gsub("\n$", "")))
end

-- JSON round trip
local v = json.decode('{"a":[1,2.5,-3e2,"x\\"y\\u00e9"],"b":{"c":true,"d":null},"e":[]}')
check(v.a[1] == 1 and v.a[2] == 2.5 and v.a[3] == -300, "json numbers")
check(v.a[4] == 'x"y\195\169', "json string escapes / unicode")
check(v.b.c == true and v.b.d == nil and #v.e == 0, "json literals and empty array")
local again = json.decode(json.encode(v))
check(again.a[4] == v.a[4] and again.b.c == true, "json encode/decode round trip")
check(json.encode({}) == "[]", "empty table encodes as array")

-- connect + hello
DC.step(10)
check(DC.client ~= nil, "connects")
check(last_sent().msg == "hello" and last_sent().theatre == "Caucasus", "sends hello")

-- spawn (line split across two receives to exercise buffering)
local spawn = json.encode({ cmd = "spawn", id = "d1", groups = {
  { country = 2, category = "ground", data = { name = "DC.d1.g0", x = 100, y = 200, units = {
    { type = "M-1 Abrams", name = "DC.d1.mbt.0", x = 100, y = 200 },
    { type = "M-1 Abrams", name = "DC.d1.mbt.1", x = 110, y = 210 } } } } } })
inbound[#inbound + 1] = { partial = spawn:sub(1, 40) }
DC.step(10.2)
check(groups["DC.d1.g0"] == nil, "waits for full line")
inbound[#inbound + 1] = { line = spawn:sub(41) }
DC.step(10.4)
check(groups["DC.d1.g0"] ~= nil, "spawns group")
local ack = last_sent()
check(ack.msg == "spawned" and ack.id == "d1" and ack.ok == true, "acks spawn")

-- failed spawn
inbound[#inbound + 1] = { line = json.encode({ cmd = "spawn", id = "bad", groups = {
  { country = 2, category = "ground", data = { name = "DC.bad.g0", units = { { type = "BOOM", name = "DC.bad.x.0" } } } } } }) }
DC.step(10.6)
ack = last_sent()
check(ack.msg == "spawned" and ack.ok == false and ack.error:find("unknown unit type"), "reports failed spawn")
check(DC.entities["bad"] == nil, "forgets failed entity")

-- route
inbound[#inbound + 1] = { line = json.encode({ cmd = "route", id = "d1", points = { { x = 1, y = 2 } } }) }
DC.step(10.8)
check(#groups["DC.d1.g0"].tasks == 1 and groups["DC.d1.g0"].tasks[1].id == "Mission", "sets route task")

-- periodic reports: players + state with one tank dead
players_by_side[2] = { (function()
  local u = make_unit("Viper-1", 5000, 6000)
  function u:getDesc() return { category = 0 } end
  function u:getPlayerName() return "Pilot" end
  function u:getTypeName() return "F-16C_50" end
  function u:getGroup() return { getID = function() return 501 end } end
  return u
end)() }
groups["DC.d1.g0"].units[2].alive = false
sent = {}
DC.step(20)
local players = json.decode((sent[1]:gsub("\n$", "")))
check(players.msg == "players" and players.players[1].id == "Pilot" and players.players[1].coalition == 2, "reports players")
local state = json.decode((sent[2]:gsub("\n$", "")))
check(state.msg == "state" and #state.entities[1].units == 1 and state.entities[1].units[1].n == "DC.d1.mbt.0", "reports alive units")

-- despawn
inbound[#inbound + 1] = { line = '{"cmd":"despawn","id":"d1"}' }
DC.step(20.2)
local gone = last_sent()
check(gone.msg == "despawned" and #gone.units == 1, "despawn reports final units")
check(groups["DC.d1.g0"] == nil and DC.entities["d1"] == nil, "despawn destroys group")

-- ground vehicles are snapped onto a nearby road, far ones left alone
land = { getClosestPointOnRoads = function(kind, x, y) return x + 50, y end }
inbound[#inbound + 1] = { line = json.encode({ cmd = "spawn", id = "d2", groups = {
  { country = 2, category = "ground", snap_to_road = true, data = { name = "DC.d2.g0", x = 0, y = 0, units = {
    { type = "M-1 Abrams", name = "DC.d2.mbt.0", x = 1000, y = 2000 } } } } } }) }
DC.step(20.3)
check(groups["DC.d2.g0"].units[1].x == 1050, "snaps vehicle to the road")
land = { getClosestPointOnRoads = function(kind, x, y) return x + 5000, y end }
inbound[#inbound + 1] = { line = json.encode({ cmd = "spawn", id = "d3", groups = {
  { country = 2, category = "ground", snap_to_road = true, data = { name = "DC.d3.g0", x = 0, y = 0, units = {
    { type = "M-1 Abrams", name = "DC.d3.mbt.0", x = 1000, y = 2000 } } } } } }) }
DC.step(20.35)
check(groups["DC.d3.g0"].units[1].x == 1000, "does not drag a vehicle to a far road")

-- SAM sites go weapons free with radars on
local alarm = {}
AI = { Option = { Ground = { id = { ALARM_STATE = 9, ROE = 0 }, val = { ALARM_STATE = { RED = 2 }, ROE = { OPEN_FIRE = 2 } } } } }
inbound[#inbound + 1] = { line = json.encode({ cmd = "spawn", id = "s1", groups = {
  { country = 0, category = "ground", alarm_red = true, data = { name = "DC.s1.g0", x = 0, y = 0, units = {
    { type = "SA-11 Buk LN 9A310M1", name = "DC.s1.launcher.0", x = 10, y = 20 } } } } } }) }
local real_add = coalition.addGroup
coalition.addGroup = function(c, cat, data)
  local g = real_add(c, cat, data)
  function g:getController() return { setOption = function(_, id, val) alarm[id] = val end, setTask = function() end } end
  return g
end
DC.step(20.37)
coalition.addGroup = real_add
check(alarm[9] == 2 and alarm[0] == 2, "SAM site set to alarm red, weapons free")

-- air spawn uses the mission-editor template and lands at home
inbound[#inbound + 1] = { line = json.encode({ cmd = "spawn", id = "fl1", groups = {
  { country = 2, category = "airplane", templates = { "DC_TPL_f16c_SEAD", "DC_TPL_f16c" }, land_at = "Kutaisi",
    data = { name = "DC.fl1.g0", task = "SEAD", units = { { type = "X", name = "DC.fl1.aircraft.0", x = 1, y = 2 },
                                                          { type = "X", name = "DC.fl1.aircraft.1", x = 1, y = 2 } },
             route = { points = { { x = 1, y = 2 }, { x = 3, y = 4 } } } } } } }) }
DC.step(20.4)
local air = groups["DC.fl1.g0"]
check(air ~= nil and air.category == 0, "spawns aeroplane group")
check(air.data.units[2].type == "F-16C_50" and air.data.units[2].payload.pylons[1].CLSID == "HARM", "applies template payload")
check(air.data.route.points[2].type == "Land" and air.data.route.points[2].airdromeId == 26, "lands at home base")

-- ATO board -> F10 menu -> join request
inbound[#inbound + 1] = { line = json.encode({ cmd = "ato", coalition = 2, version = 1, packages = {
  { id = "pk1", label = "CAS Senaki TOT D1 10:30", flights = { { id = "fl1", label = "Viper 1 F-16C SEAD (2 free)" } } } } }) }
DC.step(20.6)
local join
for _, m in ipairs(menus) do if m.label == "Join Viper 1 F-16C SEAD (2 free)" then join = m end end
check(join ~= nil and join.gid == 501, "builds ATO menu for player group")
sent = {}
join.fn(join.args)
local req = last_sent()
check(req.msg == "join" and req.flight == "fl1" and req.player == "Pilot" and req.type == "F-16C_50", "join sends request")
local count = #menus
inbound[#inbound + 1] = { line = json.encode({ cmd = "ato", coalition = 2, version = 2, packages = {} }) }
DC.step(20.8)
check(#menus < count, "rebuilds menu on new board")

-- notify: text + marks, old marks removed
inbound[#inbound + 1] = { line = json.encode({ cmd = "notify", player = "Pilot", text = "BRIEFING",
  marks = { { text = "Viper 1 TGT", x = 10, z = 20 } } }) }
DC.step(21)
check(texts[#texts].gid == 501 and texts[#texts].text == "BRIEFING", "shows text to player group")
local n = 0; for _ in pairs(marks) do n = n + 1 end
check(n == 1, "adds map mark")
inbound[#inbound + 1] = { line = json.encode({ cmd = "notify", player = "Pilot", text = "again", marks = {} }) }
DC.step(21.2)
n = 0; for _ in pairs(marks) do n = n + 1 end
check(n == 0, "replaces old marks")

-- player events: lost reported once per sortie
sent = {}
local pu = { getPlayerName = function() return "Pilot" end }
DC.event_handler:onEvent({ id = 5, initiator = pu })
DC.event_handler:onEvent({ id = 6, initiator = pu })
DC.event_handler:onEvent({ id = 9, initiator = pu })
check(#sent == 1 and last_sent().event == "lost", "reports loss once")
DC.event_handler:onEvent({ id = 3, initiator = pu })
check(last_sent().event == "takeoff", "reports takeoff")

if failures > 0 then print(failures .. " failure(s)"); os.exit(1) end
print("all bridge tests passed")
