-- Loaded by tube for the videos it plays. Records how far you got so the
-- picker can show progress: writes "<position> <duration> <ended>" to
-- <tube-progress-dir>/<tube-id> every 10s (so a crash loses at most that),
-- on quit, and when the video plays to the end.

local id = mp.get_opt("tube-id")
local dir = mp.get_opt("tube-progress-dir")
if not id or not dir then
    return
end

local utils = require("mp.utils")
local path = utils.join_path(dir, id)
local pos, dur, ended = nil, nil, false
local written = nil

local function write()
    if not pos then
        return -- never started playing (e.g. failed to load)
    end
    local line = string.format("%.3f %.3f %d\n", pos, dur or 0, ended and 1 or 0)
    if line == written then
        return
    end
    local tmp = path .. ".tmp"
    local f = io.open(tmp, "w")
    if not f then
        return
    end
    f:write(line)
    f:close()
    os.rename(tmp, path)
    written = line
end

-- time-pos and duration go nil while a file unloads; keep the last values
mp.observe_property("time-pos", "number", function(_, v)
    if v then pos = v end
end)
mp.observe_property("duration", "number", function(_, v)
    if v then dur = v end
end)

mp.add_periodic_timer(10, write)
mp.register_event("end-file", function(e)
    if e.reason == "eof" then
        ended = true
    end
    write()
end)
mp.register_event("shutdown", write)
