-- JSON-lines client for `dex serve` (the dex Python package).
--
-- One server per nvim instance, started on first use and kept alive so a
-- reopened DEX window answers instantly; VimLeavePre stops it. Replies are
-- matched to requests by id; unsolicited {"event": ...} lines go to the
-- event handler passed to start().
local M = {}

local job_id = nil
local next_request_id = 0
local callbacks = {}
local partial_line = ""
local event_handler = function(_) end

-- Tests replace this to run a scripted fake server instead of the real one.
function M.command(environment)
    local installed = vim.fn.exepath("dex")
    if installed == "" then
        installed = vim.fn.expand("~/.local/bin/dex")
        if vim.fn.executable(installed) == 0 then return nil end
    end
    -- No environment: the server opens the config's default_environment.
    if environment then return { installed, "--env", environment, "serve" } end
    return { installed, "serve" }
end

local function dispatch(line)
    if line == "" then return end
    -- JSON null stays vim.NIL: result rows are positional arrays, and nil
    -- would leave holes that shift every later column.
    local ok, message = pcall(vim.json.decode, line)
    if not ok or type(message) ~= "table" then
        event_handler({ event = "status", phase = "error", kind = "internal", message = "unreadable reply from dex: " .. line:sub(1, 120) })
        return
    end
    if message.event then
        event_handler(message)
        return
    end
    local callback = callbacks[message.id]
    callbacks[message.id] = nil
    if callback then callback(message.ok, message.ok and message.result or message.error) end
end

local function on_stdout(_, data)
    -- data is a list of chunks split on newlines; the last one is the start
    -- of a line that has not finished arriving.
    partial_line = partial_line .. data[1]
    for index = 2, #data do
        local complete = partial_line
        partial_line = data[index]
        vim.schedule(function() dispatch(complete) end)
    end
end

local function fail_pending(message)
    local pending = callbacks
    callbacks = {}
    for _, callback in pairs(pending) do
        vim.schedule(function() callback(false, { kind = "internal", message = message }) end)
    end
end

function M.running()
    return job_id ~= nil and vim.fn.jobwait({ job_id }, 0)[1] == -1
end

function M.start(handler, environment)
    event_handler = handler or event_handler
    if M.running() then return true end
    local command = M.command(environment)
    if not command then
        handler({ event = "status", phase = "error", kind = "internal",
            message = "dex is not installed: run ./install.sh or uv tool install git+https://github.com/latentspacetime/dex.git#subdirectory=backend" })
        return false
    end
    partial_line = ""
    local stderr_tail = {}
    local this_job
    this_job = vim.fn.jobstart(command, {
        on_stdout = on_stdout,
        on_stderr = function(_, data)
            for _, line in ipairs(data) do
                if line ~= "" then
                    table.insert(stderr_tail, line)
                    if #stderr_tail > 20 then table.remove(stderr_tail, 1) end
                end
            end
        end,
        on_exit = function(_, code)
            -- An environment switch stops this job and starts another before
            -- this fires; a stale exit must not clear the new job or fail
            -- its requests.
            if job_id ~= this_job then return end
            job_id = nil
            local detail = stderr_tail[#stderr_tail] or ("exit code " .. code)
            fail_pending("dex stopped: " .. detail)
            if code ~= 0 and code ~= 143 then
                vim.schedule(function()
                    event_handler({ event = "status", phase = "error", kind = "internal", message = "dex stopped: " .. detail })
                end)
            end
        end,
    })
    job_id = this_job
    if job_id <= 0 then
        job_id = nil
        handler({ event = "status", phase = "error", kind = "internal", message = "could not start dex" })
        return false
    end
    return true
end

-- callback(ok, result_or_error) runs on the main loop. Returns the request id.
function M.request(op, args, callback)
    if not M.running() then
        vim.schedule(function() callback(false, { kind = "internal", message = "dex is not running" }) end)
        return nil
    end
    next_request_id = next_request_id + 1
    local request_id = next_request_id
    callbacks[request_id] = callback
    local payload = vim.json.encode({ id = request_id, op = op, args = args or vim.empty_dict() })
    vim.fn.chansend(job_id, payload .. "\n")
    return request_id
end

function M.stop()
    if M.running() then vim.fn.jobstop(job_id) end
    job_id = nil
    -- Requests to the stopped server will never be answered.
    fail_pending("dex restarted")
    partial_line = ""
end

vim.api.nvim_create_autocmd("VimLeavePre", {
    group = vim.api.nvim_create_augroup("DexClient", { clear = true }),
    callback = M.stop,
})

return M
