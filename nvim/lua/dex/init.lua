-- DEX (<Leader>d): explore a Postgres database from a float.
--
-- Five windows share one session-lifetime state, so closing and reopening
-- resumes where you left off:
--   header   environment, safety, index age, braille spinner + status
--   prompt   the only input; its title shows the mode (SEARCH DEEP ASK SQL)
--   results  hits for the prompt (exact id, fuzzy names, deep text)
--   card     the selected row: fields, what it belongs to, what made it,
--            what links to it — or an Ask/SQL result table
--   footer   key hints for the focused window
-- The backend is `dex serve` (dex/client.lua), configured by
-- ~/.config/dex/config.toml. Every read runs in a read-only transaction,
-- and the Postgres backend refuses every write (an edited field, a typed
-- UPDATE) with an error the prompt shows.
local client = require("dex.client")
local look = require("dex.look")

local M = {}

local MODES = { "search", "deep", "ask", "sql" }
local MODE_LABELS = { search = "SEARCH", deep = "DEEP", ask = "ASK ✦", sql = "SQL" }
local MODE_HINTS = {
    search = "id, name, path, app URL, anything",
    deep = "words anywhere in comments, notes, descriptions, messages",
    ask = "ask in plain words, Enter to run",
    sql = "one read statement (dex is read-only on Postgres)",
}
local SEARCH_DEBOUNCE_MS = 30
-- The router waits for a typing pause; a verdict per keystroke would flicker.
local ROUTE_DEBOUNCE_MS = 450
-- Router verdicts map onto tabs; a change request is drafted in ASK.
local ROUTE_TARGETS = { search = "search", deep = "deep", ask = "ask", change = "ask", sql = "sql" }
local PREVIEW_DEBOUNCE_MS = 110
local SPINNER_INTERVAL_MS = 80
local RESULTS_WIDTH_RATIO = 0.4
local IMPORTANT_FIELD_LIMIT = 16
-- vim.fn.input's cancel value; a NUL cannot live in a Vimscript string.
local EDIT_CANCELLED = "\1dex-cancelled\1"

local state = {
    mode = "search",
    hits = {},
    local_hits = {},
    id_hits = {},
    selected = 1,
    query_generation = 0,
    card = nil,          -- last card payload
    table_result = nil,  -- last ask/sql payload rendered in the card window
    card_view = "home",  -- "home" | "card" | "table" | "message"
    home = nil,          -- last overview payload
    route = nil,         -- {q, mode, confidence, follow} for the prompt text
    message = nil,
    history = {},        -- drill-down stack of {table, id}
    show_all_fields = false,
    pending = 0,
    spinner_frame = 1,
    status = { phase = "starting", message = "starting dex" },
    identity = nil,
    index_age = nil,
    index_message = nil,
    login_attempted = false,
    model = nil,
    models = {},
    focus = "prompt",
    environment = nil,   -- nil: the config's default; ^t cycles environments (restarts dex serve)
    environments = {},   -- names from the config, in file order
    protected = true,    -- red chip; false shows amber
    writes = false,      -- whether the environment allows approved writes
    llm = false,         -- whether rows and typed text may reach a model (ASK, summaries)
    mode_text = {}, -- text_slot(mode) -> prompt text last typed in that mode
}

local SOURCE_SECTIONS = {
    link = { "↗ FROM APP URL", "DexSectionId" },
    id = { "≡ EXACT ID", "DexSectionId" },
    fuzzy = { "~ NAMES", "DexSectionFuzzy" },
    deep = { "¶ DEEP TEXT", "DexSectionDeep" },
}

local wins = {}
local bufs = {}
local timers = {}
local augroup = vim.api.nvim_create_augroup("Dex", { clear = true })

local function valid_win(name) return wins[name] and vim.api.nvim_win_is_valid(wins[name]) end
local function is_open() return valid_win("prompt") end

-- ── layout ───────────────────────────────────────────────────────────────
local function geometry()
    local tabline = vim.o.showtabline == 2 or (vim.o.showtabline == 1 and #vim.api.nvim_list_tabpages() > 1)
    local top = tabline and 1 or 0
    local rows = vim.o.lines - top - vim.o.cmdheight - (vim.o.laststatus == 3 and 1 or 0)
    local width = math.min(math.floor(vim.o.columns * 0.94), 220)
    local height = math.floor(rows * 0.92)
    local col = math.floor((vim.o.columns - width) / 2)
    local row = top + math.floor((rows - height) / 2)
    local results_width = math.floor((width - 4) * RESULTS_WIDTH_RATIO)
    -- header 1 + prompt 3 + body borders 2 + footer 1: the body's bottom
    -- border must end above the footer row, or it draws over the key hints.
    local body_height = height - 7
    return {
        header = { row = row, col = col, width = width, height = 1 },
        prompt = { row = row + 1, col = col, width = width - 2, height = 1 },
        results = { row = row + 4, col = col, width = results_width, height = body_height },
        card = { row = row + 4, col = col + results_width + 2, width = width - results_width - 4, height = body_height },
        footer = { row = row + height - 1, col = col, width = width, height = 1 },
    }
end

local function ensure_buf(name, filetype)
    if bufs[name] and vim.api.nvim_buf_is_valid(bufs[name]) then return bufs[name] end
    local buf = vim.api.nvim_create_buf(false, true)
    vim.bo[buf].bufhidden = "hide"
    vim.bo[buf].filetype = filetype or "dex"
    bufs[name] = buf
    return buf
end

local function window_config(name, layout)
    local base = {
        relative = "editor", row = layout.row, col = layout.col,
        width = math.max(layout.width, 10), height = math.max(layout.height, 1),
        style = "minimal", zindex = 60,
    }
    -- Every pane stays focusable: Neovim hands mouse events on a
    -- non-focusable float to the window underneath it. DEX's mouse maps
    -- (map_mouse) decide what a click or scroll does, and focus never rests
    -- on results, header, or footer.
    if name == "header" or name == "footer" then
        base.border = "none"
    else
        base.border = "rounded"
    end
    return base
end

-- ── header / footer / titles ─────────────────────────────────────────────
local function spinner_glyph()
    return state.pending > 0 and look.SPINNER[state.spinner_frame] or "◆"
end

local function render_header()
    if not valid_win("header") then return end
    local builder = look.builder()
    local chunks = {
        { " " .. spinner_glyph() .. " DEX ", "DexBrand" },
        { " " },
        { " " .. (state.environment or "…"):upper() .. " ", state.protected and "DexChipDanger" or "DexChipStaging" },
        { " " },
        { state.writes and " read-only · writes gated " or " read-only ", "DexChipSafe" },
        { " " },
        { " index " .. look.human_age(state.index_age) .. " ", "DexChipMuted" },
    }
    if state.llm and state.models and #state.models > 0 then
        table.insert(chunks, { " " })
        table.insert(chunks, { " ✦ " .. (state.model or "") .. " ", "DexChipModel" })
    elseif not state.llm then
        table.insert(chunks, { " " })
        table.insert(chunks, { " no LLM ", "DexChipMuted" })
    end
    local status_hl = ({ error = "DexError", ready = "DexDim", index_done = "DexGood" })[state.status.phase] or "DexBusy"
    local message = state.index_message or state.status.message or ""
    if state.status.phase == "error" then message = state.status.message end
    table.insert(chunks, { "  " .. message, status_hl })
    builder:line(chunks)
    builder:apply(ensure_buf("header"))
end

-- The tab the router suggests for the text now in the prompt, if it is
-- confident and different from the current tab.
local function route_follow_target()
    local route = state.route
    if not (route and route.follow) then return nil end
    if not (bufs.prompt and vim.api.nvim_buf_is_valid(bufs.prompt)) then return nil end
    if route.q ~= (vim.api.nvim_buf_get_lines(bufs.prompt, 0, 1, false)[1] or "") then return nil end
    if state.mode ~= "search" and state.mode ~= "deep" then return nil end
    local target = ROUTE_TARGETS[route.mode]
    if target == state.mode then return nil end
    return target
end

local function footer_hints()
    if state.focus == "card" then
        return {
            { "enter", "open" }, { "bs", "back" }, { "e", "edit field" }, { "y", "yank" },
            { "o", "open url" }, { "f", "all fields" }, { "r", "home" }, { "?", "all keys" }, { "i", "search" }, { "q", "close" },
        }
    end
    if state.mode == "ask" or state.mode == "sql" then
        local hints = { { "enter", "run" }, { "tab", "mode" }, { "^u", "clear" }, { "^l", "into result" }, { "^o", "model" }, { "^g", "home" }, { "esc ?", "all keys" } }
        if state.mode == "ask" then table.insert(hints, 2, { "^e", "edit last question" }) end
        return hints
    end
    return {
        { "↑↓ ^j^k", "move" }, { "enter", route_follow_target() and ("go ↪ " .. MODE_LABELS[route_follow_target()]) or "into card" },
        { "tab", "mode" }, { "^u", "clear" }, { "^g", "home" }, { "^t", "env" }, { "esc ?", "all keys" },
    }
end

local function render_footer()
    if not valid_win("footer") then return end
    local chunks = { { " " } }
    for _, hint in ipairs(footer_hints()) do
        table.insert(chunks, { " " .. hint[1] .. " ", "DexKey" })
        table.insert(chunks, { " " .. hint[2] .. "  ", "DexDim" })
    end
    local builder = look.builder()
    builder:line(chunks)
    builder:apply(ensure_buf("footer"))
end

local function prompt_title()
    local title = { { " " .. spinner_glyph() .. " ", "DexBrand" } }
    local suggested = route_follow_target()
    for _, mode in ipairs(MODES) do
        local hl = mode == state.mode and "DexModeActive" or (mode == suggested and "DexModeSuggested" or "DexModeIdle")
        local label = (mode == suggested and "↪ " or "") .. MODE_LABELS[mode]
        table.insert(title, { " " .. label .. " ", hl })
    end
    if suggested then
        local verdict = state.route.mode == "change" and "a change" or MODE_LABELS[suggested]
        table.insert(title, { string.format(" reads as %s · %.0f%% · Enter to go ", verdict, state.route.confidence * 100), "DexSuggestHint" })
    else
        table.insert(title, { " " .. MODE_HINTS[state.mode] .. " ", "DexDim" })
    end
    return title
end

local function results_title()
    local count = #state.hits
    if count == 0 then return { { " results ", "DexDim" } } end
    return { { string.format(" %d/%d ", math.min(state.selected, count), count), "DexTitle" } }
end

local function card_title()
    local crumbs = {}
    for _, entry in ipairs(state.history) do
        table.insert(crumbs, entry.table .. " #" .. tostring(entry.id))
    end
    if state.card_view == "table" then return { { " ✦ result ", "DexTitle" } } end
    if state.card_view == "home" then return { { " ◆ home ", "DexTitle" } } end
    if state.card_view == "message" and state.message then return { { " " .. state.mode .. " ", "DexTitle" } } end
    if #crumbs == 0 then return { { " card ", "DexDim" } } end
    local text = " " .. table.concat(crumbs, " › ") .. " "
    if vim.fn.strdisplaywidth(text) > 80 then text = " … › " .. crumbs[#crumbs] .. " " end
    return { { text, "DexTitle" } }
end

local function refresh_titles()
    if valid_win("prompt") then
        vim.api.nvim_win_set_config(wins.prompt, { title = prompt_title(), title_pos = "left" })
    end
    if valid_win("results") then
        -- Title: the first section's name (later sections ride as virtual
        -- lines); footer: position within the hits.
        local first = state.hits[1]
        local section = first and SOURCE_SECTIONS[first.source]
        vim.api.nvim_win_set_config(wins.results, {
            title = section and { { " " .. section[1] .. " ", section[2] } } or { { " results ", "DexDim" } },
            title_pos = "left", footer = results_title(), footer_pos = "right",
        })
    end
    if valid_win("card") then
        vim.api.nvim_win_set_config(wins.card, { title = card_title(), title_pos = "left" })
    end
end

local function refresh_chrome()
    render_header()
    render_footer()
    refresh_titles()
end

-- ── spinner ──────────────────────────────────────────────────────────────
local render_card -- defined with the card renderers below; the spinner redraws "thinking" cards
local render_summary -- defined beside render_table_view

local function spinner_tick()
    state.spinner_frame = (state.spinner_frame % #look.SPINNER) + 1
    if not is_open() then return end
    render_header()
    refresh_titles()
    if state.card_view == "message" and state.message and state.message.spinning then render_card() end
    if state.card_view == "table" and state.table_result and state.table_result.summary_pending then render_card() end
end

local function set_busy(delta)
    state.pending = math.max(0, state.pending + delta)
    if state.pending > 0 and not timers.spinner then
        timers.spinner = vim.uv.new_timer()
        timers.spinner:start(0, SPINNER_INTERVAL_MS, vim.schedule_wrap(spinner_tick))
    elseif state.pending == 0 and timers.spinner then
        timers.spinner:stop()
        timers.spinner:close()
        timers.spinner = nil
        if is_open() then refresh_chrome() end
    end
end

-- A request that keeps the spinner turning until it answers.
local function request(op, args, callback)
    set_busy(1)
    return client.request(op, args, function(ok, payload)
        set_busy(-1)
        callback(ok, payload)
    end)
end

-- ── results ──────────────────────────────────────────────────────────────

local function current_query()
    if not (bufs.prompt and vim.api.nvim_buf_is_valid(bufs.prompt)) then return "" end
    return vim.api.nvim_buf_get_lines(bufs.prompt, 0, 1, false)[1] or ""
end

local function render_results()
    if not valid_win("results") then return end
    local buf = ensure_buf("results")
    local width = vim.api.nvim_win_get_width(wins.results)
    local builder = look.builder()
    local query = current_query()
    if #state.hits == 0 then
        local hint = query == "" and "type an id, a name, a path…" or (state.pending > 0 and "searching…" or "no matches")
        builder:line({ { "  " .. hint, "DexDim" } })
        if query == "" then
            builder:blank()
            builder:line({ { "  83272", "DexKeyword" }, { "         any id, every table", "DexDim" } })
            builder:line({ { "  blue jacket", "DexKeyword" }, { "    fuzzy names", "DexDim" } })
            builder:line({ { "  gift wrap", "DexKeyword" }, { "      comments, notes, descriptions", "DexDim" } })
            builder:line({ { "  tab", "DexKey" }, { "             DEEP · ASK ✦ · SQL", "DexDim" } })
        end
        builder:apply(buf)
        refresh_titles()
        return
    end
    local id_width = 8
    local table_width = math.min(18, math.floor(width * 0.28))
    for index, hit in ipairs(state.hits) do
        local glyph, color = look.table_style(hit.table)
        local selected = index == state.selected
        local marker = selected and "▌" or " "
        -- The scope rides right-aligned as virtual text; the label stops
        -- short of it so neither overwrites the other.
        local scope_cells = (hit.scope and hit.scope ~= "") and math.min(vim.fn.strdisplaywidth(hit.scope) + 2, 22) or 0
        local label_width = math.max(8, width - table_width - id_width - 6 - scope_cells)
        local label = hit.label
        if hit.source == "deep" and hit.column ~= "" then label = hit.column .. " · " .. label end
        if hit.deleted then label = "✗ " .. label end
        local chunks = {
            { marker, "DexSelectedBar" },
            { glyph .. " ", color },
            { look.fit(hit.table, table_width) .. " ", color },
            { look.fit(hit.id, id_width) .. " ", "DexId" },
        }
        local label_start = 0
        for _, chunk in ipairs(chunks) do label_start = label_start + #chunk[1] end
        local fitted = look.fit(label, label_width)
        table.insert(chunks, { fitted, hit.deleted and "DexDeleted" or nil })
        builder:line(chunks, hit)
        if hit.source == "fuzzy" and query ~= "" then
            local ok, matched = pcall(vim.fn.matchfuzzypos, { fitted }, query)
            if ok and matched[2][1] then
                for _, character_index in ipairs(matched[2][1]) do
                    local byte_start = vim.str_byteindex(fitted, "utf-32", character_index)
                    local byte_end = vim.str_byteindex(fitted, "utf-32", character_index + 1)
                    builder:mark(label_start + byte_start, label_start + byte_end, "DexMatch")
                end
            end
        elseif hit.source == "deep" then
            local lowered = fitted:lower()
            for term in query:lower():gmatch("%S+") do
                local position = lowered:find(term, 1, true)
                if position then builder:mark(label_start + position - 1, label_start + position - 1 + #term, "DexMatch") end
            end
        end
    end
    builder:apply(buf)
    -- Section headers ride above their first hit as virtual lines, so buffer
    -- line N is always hit N.
    local previous_source = nil
    for index, hit in ipairs(state.hits) do
        if hit.source ~= previous_source then
            local section = SOURCE_SECTIONS[hit.source]
            if section and index > 1 then
                vim.api.nvim_buf_set_extmark(buf, look.namespace, index - 1, 0, {
                    virt_lines = { { { " " .. section[1] .. " ", section[2] } } }, virt_lines_above = true,
                })
            end
            previous_source = hit.source
        end
        if hit.scope and hit.scope ~= "" then
            vim.api.nvim_buf_set_extmark(buf, look.namespace, index - 1, 0, {
                virt_text = { { " " .. look.fit(hit.scope, 20):gsub("%s+$", "") .. " ", "DexBrandTag" } },
                virt_text_pos = "right_align", hl_mode = "combine",
            })
        end
    end
    vim.api.nvim_buf_set_extmark(buf, look.namespace, state.selected - 1, 0, { line_hl_group = "DexSelected", priority = 10 })
    refresh_titles()
    pcall(vim.api.nvim_win_set_cursor, wins.results, { state.selected, 0 })
end

local function merge_hits()
    local merged, seen = {}, {}
    for _, group in ipairs({ state.id_hits, state.local_hits }) do
        for _, hit in ipairs(group) do
            local key = hit.table .. "#" .. hit.id
            if not seen[key] then
                seen[key] = true
                table.insert(merged, hit)
            end
        end
    end
    state.hits = merged
    state.selected = math.max(1, math.min(state.selected, #merged))
end

-- ── card rendering ───────────────────────────────────────────────────────
local function is_null(value) return value == nil or value == vim.NIL end

local function display_value(value)
    if is_null(value) then return "∅" end
    if type(value) == "boolean" then return value and "true" or "false" end
    return look.clean(value)
end

-- Fields worth showing first: the label, status-ish columns, foreign keys,
-- and anything non-empty — but never more than IMPORTANT_FIELD_LIMIT.
local function ordered_fields(card)
    local fields = {}
    for index, column in ipairs(card.columns) do
        table.insert(fields, { column = column, value = card.values[index] })
    end
    if state.show_all_fields then return fields end
    local chosen = {}
    for _, field in ipairs(fields) do
        local value = field.value
        local empty = is_null(value) or value == "" or value == "[]" or value == "{}"
        if not empty and field.column ~= "id" then table.insert(chosen, field) end
        if #chosen >= IMPORTANT_FIELD_LIMIT then break end
    end
    return chosen
end

local function node_line(builder, node, indent, color_override)
    local glyph, color = look.table_style(node.table)
    local chunks = {
        { indent },
        { glyph .. " ", color_override or color },
        { node.table, color_override or color },
        { " #" .. tostring(node.id) .. "  ", "DexId" },
        { node.label == (node.table .. " #" .. tostring(node.id)) and "" or look.clean(node.label):sub(1, 140) },
    }
    if node.via and node.via ~= "" then table.insert(chunks, { "  ← " .. node.via, "DexDim" }) end
    if node.scope and node.scope ~= "" then table.insert(chunks, { "  " .. node.scope, "DexBrandTag" }) end
    builder:line(chunks, { kind = "node", table = node.table, id = node.id })
end

-- Enter on a group header opens its newest item.
local function group_data(group)
    local first = group.items and group.items[1]
    if not first then return nil end
    return { kind = "node", table = first.table, id = first.id }
end

local function section(builder, title, hl, count)
    builder:blank()
    local chunks = { { " " .. title .. " ", hl } }
    if count then table.insert(chunks, { "  " .. count, "DexDim" }) end
    builder:line(chunks)
end

local function render_card_view(card)
    local builder = look.builder()
    local glyph, color = look.table_style(card.table)
    builder:line({
        { " " .. glyph .. " ", color },
        { look.heading_name(card.table), "DexHeading" },
        { "  #" .. tostring(card.id), "DexIdStrong" },
        -- A row with no label column gets "table #id" as its label; showing
        -- it again beside the id says nothing.
        { card.label == (card.table .. " #" .. tostring(card.id)) and "" or ("   " .. look.clean(card.label):sub(1, 90)), "DexTitleText" },
    })
    local meta = { { "   " } }
    if card.scope and card.scope ~= "" then table.insert(meta, { " " .. card.scope .. " ", "DexChipBrand" }) end
    table.insert(meta, { "  " .. tostring(card.elapsed_ms) .. " ms", "DexDim" })
    builder:line(meta)

    local fields = ordered_fields(card)
    local key_width = 4
    for _, field in ipairs(fields) do key_width = math.max(key_width, math.min(28, #field.column)) end
    section(builder, "FIELDS", "DexSectionFields", state.show_all_fields and "all" or ("f: all " .. #card.columns))
    for _, field in ipairs(fields) do
        local value = display_value(field.value)
        local value_hl = is_null(field.value) and "DexDim" or (value == "‹redacted›" and "DexError" or "DexValue")
        builder:line({
            { "   " .. look.fit(field.column, key_width) .. "  ", "DexFieldKey" },
            { value:sub(1, 300), value_hl },
        }, { kind = "field", table = card.table, id = card.id, column = field.column, value = field.value })
    end

    if #card.parents > 0 then
        section(builder, "↑ BELONGS TO", "DexSectionParents", #card.parents)
        for _, node in ipairs(card.parents) do node_line(builder, node, "   ") end
    end
    if #card.lineage > 0 then
        section(builder, "⚗ MADE FROM", "DexSectionLineage", "lineage from the config")
        for depth, node in ipairs(card.lineage) do
            node_line(builder, node, "   " .. string.rep("  ", depth - 1) .. "↳ ")
        end
    end
    if #card.linked > 0 then
        section(builder, "⇄ LINKED", "DexSectionLinked")
        for _, group in ipairs(card.linked) do
            local group_glyph, group_color = look.table_style(group.table)
            builder:line({ { "   " .. group_glyph .. " ", group_color }, { group.table, group_color }, { "  ×" .. group.count, "DexCount" }, { "  via " .. group.via, "DexDim" } },
                group_data(group))
            for _, node in ipairs(group.items) do node_line(builder, node, "      ") end
        end
    end
    if #card.children > 0 then
        section(builder, "↓ LINKS HERE", "DexSectionChildren", #card.children .. " tables")
        for _, group in ipairs(card.children) do
            local group_glyph, group_color = look.table_style(group.table)
            builder:line({ { "   " .. group_glyph .. " ", group_color }, { group.table, group_color }, { "  ×" .. group.count, "DexCount" }, { "  via " .. group.via, "DexDim" } },
                group_data(group))
            for _, node in ipairs(group.items) do node_line(builder, node, "      ") end
        end
    end
    return builder
end

-- Word-wrap text to the card's width for prose sections.
local function wrap(text, width)
    local lines, current = {}, ""
    for word in tostring(text):gsub("%s+", " "):gmatch("%S+") do
        if current ~= "" and vim.fn.strdisplaywidth(current .. " " .. word) > width then
            table.insert(lines, current)
            current = word
        else
            current = current == "" and word or (current .. " " .. word)
        end
    end
    if current ~= "" then table.insert(lines, current) end
    return lines
end

render_summary = function(builder, result)
    if not (result.summary or result.summary_pending or result.summary_error) then return builder end
    builder:blank()
    local glyph = result.summary_pending and state.pending > 0 and (look.SPINNER[state.spinner_frame] .. " ") or ""
    local meta = result.summary_ms and string.format("  %s · %.1fs", result.summary_model or "", result.summary_ms / 1000) or ""
    builder:line({ { " " .. glyph .. "✦ SUMMARY ", "DexSectionLineage" }, { meta, "DexDim" } })
    if result.summary_pending then
        builder:line({ { "   reading the rows…", "DexDim" } })
    elseif result.summary_error then
        builder:line({ { "   " .. result.summary_error, "DexError" } })
    else
        local width = valid_win("card") and (vim.api.nvim_win_get_width(wins.card) - 6) or 90
        for _, line in ipairs(wrap(result.summary, math.max(width, 30))) do builder:line({ { "   " .. line, "DexHeading" } }) end
    end
    return builder
end

local function render_table_view(result)
    local builder = look.builder()
    local header = { { " ✦ ", "DexBrand" } }
    if result.model and result.model ~= "" then table.insert(header, { " " .. result.model .. " ", "DexChipModel" }) end
    if result.llm_ms and result.llm_ms > 0 then table.insert(header, { string.format("  llm %.1fs", result.llm_ms / 1000), "DexDim" }) end
    if result.route_ms and result.route_ms > 0 then
        table.insert(header, { string.format("  route %s %.0f%% %.0fms", result.route, (result.route_confidence or 0) * 100, result.route_ms), "DexDim" })
    end
    if result.sql_ms or result.elapsed_ms then table.insert(header, { string.format("  sql %.0fms", result.sql_ms or result.elapsed_ms), "DexDim" }) end
    if result.cost_usd and result.cost_usd > 0 then table.insert(header, { string.format("  $%.4f", result.cost_usd), "DexDim" }) end
    builder:line(header)
    if result.question then builder:line({ { "   ❯ ", "DexBrandText" }, { result.question, "DexHeading" } }) end
    if result.sql and result.sql ~= "" then
        builder:blank()
        for _, sql_line in ipairs(vim.split(result.sql, "\n", { plain = true })) do
            local number = builder:line({ { "   " }, { sql_line, "DexSql" } })
            look.sql_marks(builder, number, sql_line, 3)
        end
    end
    if result.error and result.error ~= "" then
        builder:blank()
        builder:line({ { " ✗ ", "DexChipDanger" }, { " " .. result.error, "DexError" } })
        return builder
    end
    local columns, rows = result.columns or {}, result.rows or {}
    if result.checks and #result.checks > 0 then
        builder:blank()
        builder:line({ { " ⚠ CHECKED ", "DexSectionId" }, { "  the first query came back empty; its filters against the data:", "DexDim" } })
        for _, line in ipairs(result.checks) do builder:line({ { "   · " .. line, "DexWarn" } }) end
    end
    builder:blank()
    builder:line({ { " ROWS ", "DexSectionFields" }, { "  " .. #rows .. (result.truncated and "+ (cut)" or ""), "DexDim" } })
    if #columns == 0 then return render_summary(builder, result) end
    local widths = {}
    for index, column in ipairs(columns) do
        local width = vim.fn.strdisplaywidth(column)
        for row_index = 1, math.min(#rows, 200) do
            width = math.max(width, vim.fn.strdisplaywidth(display_value(rows[row_index][index])))
        end
        widths[index] = math.min(width, 48)
    end
    local header_chunks = { { "   " } }
    local rule = "   "
    for index, column in ipairs(columns) do
        local mapped = result.column_tables and result.column_tables[column]
        table.insert(header_chunks, { look.fit(column, widths[index]) .. "  ", mapped and "DexColumnLink" or "DexColumn" })
        rule = rule .. string.rep("─", widths[index]) .. "  "
    end
    builder:line(header_chunks)
    builder:line({ { rule, "DexRule" } })
    for row_index, row in ipairs(rows) do
        local chunks = { { "   " } }
        local drill = nil
        for index, column in ipairs(columns) do
            local value = row[index]
            local mapped = result.column_tables and result.column_tables[column]
            if mapped and not drill and not is_null(value) then drill = { kind = "node", table = mapped, id = value } end
            local hl = is_null(value) and "DexDim" or (mapped and "DexId" or (row_index % 2 == 0 and "DexValueAlt" or "DexValue"))
            table.insert(chunks, { look.fit(display_value(value), widths[index]) .. "  ", hl })
        end
        builder:line(chunks, drill)
    end
    return render_summary(builder, result)
end

local function thousands(number)
    local text = tostring(math.floor(number or 0))
    local formatted = text:reverse():gsub("(%d%d%d)", "%1,"):reverse()
    return (formatted:gsub("^,", ""))
end

local SPARK = { "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█" }
local BAR_COLORS = { "DexC13", "DexC12", "DexC14", "DexC10", "DexC11", "DexC9" }

-- "2026-10-12 13:59:53" (UTC) → "4d", "3h", "12m".
local function age(timestamp)
    local year, month, day, hour, minute, second = tostring(timestamp):match("(%d+)-(%d+)-(%d+)[ T](%d+):(%d+):(%d+)")
    if not year then return "" end
    local then_time = os.time({ year = year, month = month, day = day, hour = hour, min = minute, sec = second })
    local seconds = os.time(os.date("!*t")) - then_time
    if seconds < 3600 then return math.max(0, math.floor(seconds / 60)) .. "m" end
    if seconds < 86400 then return math.floor(seconds / 3600) .. "h" end
    return math.floor(seconds / 86400) .. "d"
end

local function render_home_view(home)
    local builder = look.builder()
    builder:line({ { " ◆ DEX ", "DexBrand" }, { "  " .. (state.database or "") .. " · " .. (state.environment or ""), "DexHeading" },
        { state.writes and "  · read-only · writes gated" or "  · read-only", "DexDim" },
        { string.format("    %s ms", tostring(home.elapsed_ms or "")), "DexDim" } })
    builder:blank()

    local activity = home.activity or {}
    if #activity > 0 then
        builder:line({ { " ACTIVITY ", "DexSectionFields" }, { "                    24h        7d", "DexDim" } })
        local per_day = home.per_day or {}
        local peak = 1
        for _, day in ipairs(per_day) do peak = math.max(peak, day.n) end
        local spark = ""
        for _, day in ipairs(per_day) do spark = spark .. SPARK[math.max(1, math.ceil(day.n / peak * #SPARK))] end
        for index, row in ipairs(activity) do
            local glyph, color = look.table_style(row.label)
            local chunks = {
                { "   " .. glyph .. " ", color }, { look.fit(row.label, 18), "DexFieldKey" },
                { string.format("%8s", thousands(row.day)), "DexIdStrong" },
                { string.format("%10s", thousands(row.week)), "DexValue" },
            }
            if index == 1 and spark ~= "" then
                table.insert(chunks, { "    " .. spark, "DexC14" })
                table.insert(chunks, { "  per day, " .. #per_day .. "d", "DexDim" })
            end
            builder:line(chunks)
        end
    end

    local scopes = home.top_scopes or {}
    if #scopes > 0 then
        section(builder, "◆ MOST ACTIVE " .. home.scope_table:upper(), "DexSectionParents", home.per_day_label .. " this week")
        local most = scopes[1].n
        for index, scope in ipairs(scopes) do
            local width = math.max(1, math.floor(scope.n / most * 28))
            builder:line({
                { "   " .. look.fit(scope.name, 26), "DexValue" },
                { string.rep("█", width), BAR_COLORS[((index - 1) % #BAR_COLORS) + 1] },
                { " " .. thousands(scope.n), "DexCount" },
            }, { kind = "node", table = home.scope_table, id = scope.id })
        end
    end

    for _, group in ipairs(home.recent or {}) do
        if #group.rows > 0 then
            local glyph, color = look.table_style(group.table)
            section(builder, glyph .. " NEWEST " .. group.table:upper(), "DexSectionChildren")
            for _, row in ipairs(group.rows) do
                builder:line({
                    { "   " .. glyph .. " ", color }, { string.format("%-5s", age(row.at)), "DexDim" },
                    { "#" .. tostring(row.id) .. " ", "DexId" }, { look.fit(row.label, 50) .. " ", "DexValue" },
                    { look.fit(row.scope, 20), "DexBrandTag" },
                }, { kind = "node", table = group.table, id = row.id })
            end
        end
    end

    local tables = home.largest_tables or {}
    if #tables > 0 then
        section(builder, "≡ LARGEST TABLES", "DexSectionLinked")
        local largest = math.max(1, tables[1].rows)
        for _, entry in ipairs(tables) do
            local glyph, color = look.table_style(entry.table)
            builder:line({
                { "   " .. glyph .. " ", color }, { look.fit(entry.table, 26), color },
                { string.rep("▪", math.max(1, math.floor(entry.rows / largest * 20))), "DexRule" },
                { " ~" .. thousands(entry.rows), "DexDim" },
            })
        end
    end
    builder:blank()
    builder:line({ { "   type an id, name, path or app URL · ", "DexDim" }, { "Tab", "DexKey" }, { " DEEP · ASK ✦ · SQL · ", "DexDim" },
        { "Enter", "DexKey" }, { " on a line opens it", "DexDim" } })
    return builder
end

render_card = function()
    if not valid_win("card") then return end
    local builder
    if state.card_view == "home" and state.home then
        builder = render_home_view(state.home)
    elseif state.card_view == "card" and state.card then
        builder = render_card_view(state.card)
    elseif state.card_view == "table" and state.table_result then
        builder = render_table_view(state.table_result)
    elseif state.card_view == "message" and state.message then
        builder = look.builder()
        local glyph = state.message.spinning and state.pending > 0 and (look.SPINNER[state.spinner_frame] .. " ") or ""
        builder:line({ { " " .. glyph .. state.message.title .. " ", state.message.hl or "DexChipMuted" } })
        builder:blank()
        for _, text in ipairs(vim.split(state.message.body or "", "\n", { plain = true })) do builder:line({ { "  " .. text } }) end
    else
        -- Home before its first overview arrives.
        builder = look.builder()
        local glyph = state.pending > 0 and look.SPINNER[state.spinner_frame] or "◆"
        builder:line({ { " " .. glyph .. " DEX ", "DexBrand" }, { "  reading the database…", "DexDim" } })
    end
    builder:apply(ensure_buf("card"))
    state.card_line_data = builder.line_data
    refresh_titles()
end

-- ── data flow ────────────────────────────────────────────────────────────
local function show_error(title, payload)
    state.card_view = "message"
    state.message = { title = "✗ " .. title, hl = "DexChipDanger", body = (payload and payload.message) or tostring(payload) }
    render_card()
end

local function load_card(table_name, id, push_history)
    -- Cards race: a heavy card can answer after a newer light one. Only the
    -- latest request may render.
    state.card_generation = (state.card_generation or 0) + 1
    local generation = state.card_generation
    request("card", { table = table_name, id = id }, function(ok, payload)
        if generation ~= state.card_generation then return end
        if not ok then
            show_error(table_name .. " #" .. tostring(id), payload)
            return
        end
        if push_history then
            table.insert(state.history, { table = table_name, id = id })
        else
            state.history = { { table = table_name, id = id } }
        end
        state.card, state.card_view = payload, "card"
        render_card()
        if valid_win("card") then pcall(vim.api.nvim_win_set_cursor, wins.card, { 1, 0 }) end
    end)
end

local function load_home()
    request("home", {}, function(ok, payload)
        if not ok then return end
        state.home = payload
        if state.card_view == "home" then render_card() end
    end)
end

local function show_home()
    state.history, state.card = {}, nil
    state.card_view = "home"
    render_card()
    load_home()
end

-- After a search finishes with nothing: never leave the previous card up.
local function show_no_matches(query)
    if vim.trim(query) == "" then return show_home() end
    state.history, state.card = {}, nil
    state.card_view = "message"
    state.message = {
        title = "no matches", hl = "DexChipMuted",
        body = "Nothing in the index or the id lookup matches \"" .. query .. "\".\n"
            .. "Tab to DEEP to search inside text, or ASK ✦ to answer it with SQL.",
    }
    render_card()
end

local function request_route(query)
    if timers.route then timers.route:stop() end
    if not query:find("%s") then
        -- Single tokens, ids, paths and URLs stay in SEARCH; no model call.
        state.route = nil
        return
    end
    timers.route = timers.route or vim.uv.new_timer()
    timers.route:start(ROUTE_DEBOUNCE_MS, 0, vim.schedule_wrap(function()
        if current_query() ~= query then return end
        client.request("route", { q = query }, function(ok, payload)
            if not ok or current_query() ~= query then return end
            state.route = payload
            refresh_titles()
            render_footer()
        end)
    end))
end

local function schedule_preview()
    if timers.preview then timers.preview:stop() end
    timers.preview = timers.preview or vim.uv.new_timer()
    local hit = state.hits[state.selected]
    if not hit then return end
    timers.preview:start(PREVIEW_DEBOUNCE_MS, 0, vim.schedule_wrap(function()
        local still = state.hits[state.selected]
        if still and still.table == hit.table and still.id == hit.id and is_open() then
            load_card(hit.table, hit.id, false)
        end
    end))
end

local function run_search()
    local query = current_query()
    state.searched_key = state.mode .. "\0" .. query
    state.query_generation = state.query_generation + 1
    local generation = state.query_generation
    state.selected = 1
    state.route = nil
    if vim.trim(query) == "" then
        state.local_hits, state.id_hits = {}, {}
        merge_hits()
        render_results()
        refresh_titles()
        render_footer()
        if state.card_view ~= "home" then show_home() end
        return
    end
    request_route(query)
    local outstanding = state.mode == "search" and 2 or 1
    local failure = nil
    local function settle(ok, payload)
        outstanding = outstanding - 1
        if not ok then failure = failure or payload end
        merge_hits()
        render_results()
        if #state.hits > 0 then
            schedule_preview()
        elseif outstanding == 0 then
            -- A lookup that failed (still connecting, tunnel down) is not an
            -- empty answer; say what happened instead of "no matches".
            if failure then show_error("search", failure) else show_no_matches(query) end
        end
    end
    local mode = state.mode == "deep" and "deep" or "auto"
    request("search", { q = query, mode = mode }, function(ok, payload)
        if generation ~= state.query_generation then return end
        state.local_hits = ok and payload.hits or {}
        settle(ok, payload)
    end)
    if state.mode == "search" then
        request("lookup", { q = query }, function(ok, payload)
            if generation ~= state.query_generation then return end
            state.id_hits = ok and payload.hits or {}
            settle(ok, payload)
        end)
    else
        state.id_hits = {}
    end
end

local function on_prompt_changed()
    -- The prompt is one line; a multi-line paste is joined, not split.
    if vim.api.nvim_buf_line_count(bufs.prompt) > 1 then
        local joined = table.concat(vim.api.nvim_buf_get_lines(bufs.prompt, 0, -1, false), " ")
        vim.api.nvim_buf_set_lines(bufs.prompt, 0, -1, false, { joined })
        if valid_win("prompt") then vim.api.nvim_win_set_cursor(wins.prompt, { 1, #joined }) end
    end
    if state.mode ~= "search" and state.mode ~= "deep" then return end
    -- Leaving insert mode fires TextChanged for text TextChangedI already
    -- searched; re-searching would reset the selection to the first hit.
    if state.searched_key == state.mode .. "\0" .. current_query() then return end
    if timers.search then timers.search:stop() end
    timers.search = timers.search or vim.uv.new_timer()
    timers.search:start(SEARCH_DEBOUNCE_MS, 0, vim.schedule_wrap(run_search))
end

-- Keys wrap around the list; the scroll wheel clamps (clamp = true), since a
-- wheel that jumps from the last hit back to the first feels broken.
local function move(delta, clamp)
    if #state.hits == 0 then return end
    if clamp then
        state.selected = math.max(1, math.min(#state.hits, state.selected + delta))
    else
        state.selected = ((state.selected - 1 + delta) % #state.hits) + 1
    end
    render_results()
    schedule_preview()
end

-- ── writes: the approval modal ───────────────────────────────────────────
-- TODO(dex-postgres): unreachable while the Postgres backend is read-only (it
-- never returns a plan). Delete this modal, handle_write_payload and the
-- "writes gated" chips, or bring them back with a Postgres write gate.
local approval = { win = nil, buf = nil, plan = nil, stage = nil }

local function close_approval()
    if approval.win and vim.api.nvim_win_is_valid(approval.win) then vim.api.nvim_win_close(approval.win, true) end
    approval.win = nil
    if valid_win("prompt") then vim.api.nvim_set_current_win(wins.prompt) end
end

local function render_approval(outcome)
    local plan = approval.plan
    local builder = look.builder()
    builder:line({ { " ⚠ ", "DexChipDanger" }, { "  " .. plan.kind:upper() .. " ", "DexHeading" }, { plan.environment .. "." .. plan.table, "DexDangerText" },
        { "   " .. plan.expected_rows .. " row(s)", "DexCount" } })
    builder:blank()
    for _, warning in ipairs(plan.warnings or {}) do builder:line({ { "  ! " .. warning, "DexWarn" } }) end
    builder:blank()
    for _, sql_line in ipairs(vim.split(plan.sql, "\n", { plain = true })) do
        local number = builder:line({ { "  " }, { sql_line, "DexSql" } })
        look.sql_marks(builder, number, sql_line, 2)
    end
    builder:blank()
    builder:line({ { " PREVIEW ", "DexSectionFields" }, { "  computed on the read replica; nothing has changed", "DexDim" } })
    local columns = plan.preview_columns or {}
    local widths = {}
    for index, column in ipairs(columns) do
        local width = vim.fn.strdisplaywidth(column)
        for _, row in ipairs(plan.preview_rows or {}) do width = math.max(width, vim.fn.strdisplaywidth(display_value(row[index]))) end
        widths[index] = math.min(width, 40)
    end
    local header = { { "  " } }
    for index, column in ipairs(columns) do
        local hl = column:find("· new$") and "DexGood" or (column:find("· now$") and "DexDangerText" or "DexColumn")
        table.insert(header, { look.fit(column, widths[index]) .. "  ", hl })
    end
    builder:line(header)
    for _, row in ipairs(plan.preview_rows or {}) do
        local chunks = { { "  " } }
        for index, column in ipairs(columns) do
            local hl = column:find("· new$") and "DexGood" or (column:find("· now$") and "DexDangerText" or "DexValue")
            table.insert(chunks, { look.fit(display_value(row[index]), widths[index]) .. "  ", hl })
        end
        builder:line(chunks)
    end
    builder:blank()
    if outcome then
        if outcome.committed then
            builder:line({ { " ✓ ", "DexChipSafe" }, { "  " .. outcome.message, "DexGood" } })
        else
            builder:line({ { " ✗ ", "DexChipDanger" }, { "  " .. outcome.message, "DexError" } })
        end
        builder:line({ { "  q close", "DexDim" } })
    elseif approval.stage == "review" then
        builder:line({ { " a ", "DexKeyDanger" }, { " approve this preview   ", "DexDangerText" }, { " q ", "DexKey" }, { " cancel", "DexDim" } })
    else
        builder:line({ { "  executing…", "DexBusy" } })
    end
    builder:apply(approval.buf)
    vim.api.nvim_win_set_config(approval.win, {
        title = { { " ⚠ WRITE GATE · " .. plan.environment:upper() .. " ", "DexChipDanger" } }, title_pos = "center",
    })
end

local function confirm_and_execute()
    local plan = approval.plan
    vim.cmd("redraw")
    local ok, typed = pcall(vim.fn.input, {
        prompt = "Type '" .. plan.confirmation_phrase .. "' to write to " .. plan.environment:upper() .. ": ",
        cancelreturn = "",
    })
    if not ok or vim.trim(typed) ~= plan.confirmation_phrase then
        request("discard_write", { token = plan.token }, function() end)
        render_approval({ committed = false, message = "confirmation did not match; nothing changed" })
        return
    end
    approval.stage = "executing"
    render_approval()
    request("execute_write", { token = plan.token, confirmation = typed }, function(executed, payload)
        if not executed then
            render_approval({ committed = false, message = payload.message })
            return
        end
        render_approval(payload)
        if payload.committed and state.card and state.card.table == plan.table then
            load_card(state.card.table, state.card.id, false)
        end
    end)
end

local function open_approval(plan)
    approval.plan, approval.stage = plan, "review"
    approval.buf = approval.buf and vim.api.nvim_buf_is_valid(approval.buf) and approval.buf or vim.api.nvim_create_buf(false, true)
    local width = math.min(vim.o.columns - 8, 120)
    local height = math.min(vim.o.lines - 6, 14 + #(plan.preview_rows or {}) + #(plan.warnings or {}))
    approval.win = vim.api.nvim_open_win(approval.buf, true, {
        relative = "editor", row = math.floor((vim.o.lines - height) / 2), col = math.floor((vim.o.columns - width) / 2),
        width = width, height = height, style = "minimal", border = "double", zindex = 90,
    })
    vim.wo[approval.win].winhighlight = "FloatBorder:DexDangerBorder,NormalFloat:DexApprovalNormal"
    -- Opened from the prompt's insert mode; the gate's keys are normal-mode.
    vim.cmd("stopinsert")
    local options = { buffer = approval.buf, nowait = true, silent = true }
    vim.keymap.set("n", "a", function()
        if approval.stage == "review" then confirm_and_execute() end
    end, options)
    for _, key in ipairs({ "q", "<Esc>" }) do
        vim.keymap.set("n", key, function()
            if approval.stage == "review" then request("discard_write", { token = plan.token }, function() end) end
            close_approval()
        end, options)
    end
    render_approval()
end

local function handle_write_payload(payload)
    if payload.plan then
        open_approval(payload.plan)
        return true
    end
    return false
end

-- ── ask / sql ────────────────────────────────────────────────────────────
local function set_prompt(text)
    vim.api.nvim_buf_set_lines(bufs.prompt, 0, -1, false, { text })
    if valid_win("prompt") then pcall(vim.api.nvim_win_set_cursor, wins.prompt, { 1, #text }) end
end

-- The rows are on screen first; a plain-English answer lands under them.
local function request_summary(result, question)
    -- An environment with llm = false never sends rows to a model.
    if not state.llm then return end
    if (result.error and result.error ~= "") or not result.columns or #result.columns == 0 then return end
    result.summary_pending = true
    render_card()
    request("summarize", {
        question = question, sql = result.sql, columns = result.columns, rows = result.rows,
        truncated = result.truncated, checks = result.checks,
    }, function(ok, payload)
        result.summary_pending = false
        if ok then
            result.summary, result.summary_model, result.summary_ms = payload.summary, payload.model, payload.elapsed_ms
        else
            result.summary_error = "summary unavailable: " .. (payload.message or "")
        end
        if state.table_result == result and state.card_view == "table" then render_card() end
    end)
end

local function run_statement()
    local text = vim.trim(current_query())
    if text == "" and state.mode == "ask" and state.lookup_verdict_for then
        -- Enter on the cleared prompt right after a lookup verdict: answer
        -- that question with SQL after all.
        text = state.lookup_verdict_for
    end
    if text == "" then return end
    if state.mode == "ask" then
        -- The question moves into the result header; the prompt is ready for
        -- the next one, and ^e brings this one back to edit.
        state.last_question = text
        set_prompt("")
    end
    state.card_view = "message"
    state.message = { title = state.mode == "ask" and "thinking" or "running", hl = "DexChipModel", body = text, spinning = true }
    render_card()
    if state.mode == "ask" then
        -- Enter on the same text right after a lookup verdict means "ask anyway".
        local force = state.lookup_verdict_for == text
        state.lookup_verdict_for = nil
        request("ask", { question = text, model = state.model, force = force }, function(ok, payload)
            if not ok then return show_error("ask", payload) end
            if payload.search then
                -- The router read this as a lookup: hits on the left, and the
                -- card says how to get an SQL answer instead.
                state.local_hits, state.id_hits = payload.search.hits, {}
                state.selected = 1
                merge_hits()
                render_results()
                state.lookup_verdict_for = text
                state.card_view = "message"
                state.message = {
                    title = string.format("≡ read as a lookup · %.0f%%", (payload.route_confidence or 0) * 100),
                    hl = "DexSectionId",
                    body = #state.hits .. " matches on the left (^j/^k to move).\nPress Enter to answer it with SQL instead, or ^e to edit it.",
                }
                render_card()
                return
            end
            if handle_write_payload(payload) then return end
            state.table_result, state.card_view = payload, "table"
            render_card()
            request_summary(payload, text)
        end)
    else
        request("sql", { sql = text }, function(ok, payload)
            if not ok then return show_error("sql", payload) end
            if handle_write_payload(payload) then return end
            state.table_result, state.card_view = payload, "table"
            render_card()
            request_summary(payload, text)
        end)
    end
end

-- ── card actions ─────────────────────────────────────────────────────────
local function line_data_under_cursor()
    if not valid_win("card") or not state.card_line_data then return nil end
    local line = vim.api.nvim_win_get_cursor(wins.card)[1]
    return state.card_line_data[line]
end

local function focus(name)
    if not valid_win(name) then return end
    state.focus = name
    vim.api.nvim_set_current_win(wins[name])
    if name == "prompt" then vim.cmd("startinsert!") else vim.cmd("stopinsert") end
    render_footer()
end

local function drill()
    local data = line_data_under_cursor()
    if data and data.kind == "node" then load_card(data.table, data.id, true) end
end

local function back()
    if #state.history <= 1 then return end
    table.remove(state.history)
    local previous = table.remove(state.history)
    load_card(previous.table, previous.id, true)
end

local function yank()
    local data = line_data_under_cursor()
    local text
    if data and data.kind == "field" then
        text = display_value(data.value)
    elseif data and data.kind == "node" then
        text = data.table .. " #" .. tostring(data.id)
    elseif state.card then
        text = vim.json.encode(state.card)
    end
    if text then _G.copy_to_system_clipboard(text) end
end

local function open_url()
    local data = line_data_under_cursor()
    local value = data and (data.value or (data.kind == "node" and nil))
    if type(value) == "string" and value:match("^https?://") then
        vim.ui.open(value)
    elseif type(value) == "string" and value:match("^s3://") then
        local bucket, key = value:match("^s3://([^/]+)/(.+)$")
        if bucket then vim.ui.open("https://s3.console.aws.amazon.com/s3/object/" .. bucket .. "?region=us-east-2&prefix=" .. key) end
    else
        vim.notify("DEX: no URL on this line", vim.log.levels.INFO)
    end
end

local function edit_field()
    local data = line_data_under_cursor()
    if not (data and data.kind == "field") then
        vim.notify("DEX: put the cursor on a field line to edit it", vim.log.levels.INFO)
        return
    end
    local current = is_null(data.value) and "" or tostring(data.value)
    local ok, typed = pcall(vim.fn.input, {
        prompt = data.table .. "." .. data.column .. " = ", default = current, cancelreturn = EDIT_CANCELLED,
    })
    if not ok or typed == EDIT_CANCELLED or typed == current then return end
    local value = typed == "\\N" and vim.NIL or typed
    request("edit_field", { table = data.table, id = data.id, column = data.column, value = value }, function(planned, payload)
        if not planned then return show_error("edit " .. data.column, payload) end
        handle_write_payload(payload)
    end)
end

-- SEARCH and DEEP share text (same words, wider net); ASK and SQL each keep
-- their own, so a long question never lands in the search box.
local function text_slot(mode) return mode == "deep" and "search" or mode end

local function cycle_mode(delta)
    local index = 1
    for position, mode in ipairs(MODES) do if mode == state.mode then index = position end end
    local previous = state.mode
    state.mode = MODES[((index - 1 + delta) % #MODES) + 1]
    if text_slot(previous) ~= text_slot(state.mode) then
        state.mode_text[text_slot(previous)] = current_query()
        local restored = state.mode_text[text_slot(state.mode)] or ""
        vim.api.nvim_buf_set_lines(bufs.prompt, 0, -1, false, { restored })
        if valid_win("prompt") then pcall(vim.api.nvim_win_set_cursor, wins.prompt, { 1, #restored }) end
    end
    refresh_chrome()
    if state.mode == "search" or state.mode == "deep" then run_search() end
end

-- Move the prompt text to the routed tab and run it there.
local function follow_route(target)
    local text = current_query()
    state.route = nil
    if text_slot(target) ~= text_slot(state.mode) then
        state.mode_text[text_slot(state.mode)] = "" -- the text moves, it is not copied
    end
    state.mode = target
    refresh_chrome()
    if target == "deep" or target == "search" then
        state.searched_key = nil
        run_search()
    else
        -- The search hits were for this text as a search; they do not belong
        -- beside its answer.
        state.local_hits, state.id_hits = {}, {}
        merge_hits()
        render_results()
        run_statement()
    end
end

local function on_prompt_enter()
    if state.mode == "ask" or state.mode == "sql" then return run_statement() end
    local target = route_follow_target()
    if target then return follow_route(target) end
    local text = current_query()
    if text:find("%s") and not (state.route and state.route.q == text) then
        -- Enter beat the typing-pause router: ask it now, then act.
        return request("route", { q = text }, function(ok, payload)
            if ok and current_query() == text then
                state.route = payload
                local routed = route_follow_target()
                if routed then return follow_route(routed) end
            end
            if state.card then focus("card") end
        end)
    end
    if state.card then focus("card") end
end

local function clear_prompt()
    set_prompt("")
    state.mode_text[text_slot(state.mode)] = ""
    state.route = nil
    state.lookup_verdict_for = nil
    if state.mode == "search" or state.mode == "deep" then
        run_search()
    else
        refresh_chrome()
    end
end

local function go_home()
    set_prompt("")
    state.mode_text[text_slot(state.mode)] = ""
    state.route = nil
    state.local_hits, state.id_hits = {}, {}
    merge_hits()
    render_results()
    state.searched_key = state.mode .. "\0"
    show_home()
    refresh_chrome()
    focus("prompt")
end

local function cycle_model()
    if #state.models == 0 then return end
    local index = 1
    for position, model in ipairs(state.models) do if model == state.model then index = position end end
    state.model = state.models[(index % #state.models) + 1]
    render_header()
end

-- ── server events ────────────────────────────────────────────────────────
local function on_event(event)
    if event.phase == "index" then
        state.index_message = (look.SPINNER[state.spinner_frame]) .. " indexing · " .. (event.message or "")
    elseif event.phase == "index_done" then
        state.index_message = nil
        state.index_age = 0
        state.status = { phase = "index_done", message = "✓ " .. event.message }
    else
        state.status = { phase = event.phase, message = event.message, kind = event.kind }
        if event.phase == "ready" or event.phase == "error" then
            if state.awaiting_ready then
                state.awaiting_ready = false
                set_busy(-1)
            end
        end
        if event.phase == "ready" then
            state.identity = event.identity
            state.index_age = event.index_age
        elseif event.phase == "error" then
            if event.kind == "auth" and state.can_login and not state.login_attempted then
                -- Login expired: run the config's login_command once per session.
                state.login_attempted = true
                state.status = { phase = "login", message = "login expired · running login_command; approve it if a browser opens" }
                request("login", {}, function(ok, payload)
                    if not ok then state.status = { phase = "error", message = "login failed: " .. payload.message } end
                    if is_open() then render_header() end
                end)
            end
        end
    end
    if is_open() then render_header() end
end

local function ensure_server()
    if client.running() then return end
    state.awaiting_ready = true
    set_busy(1) -- released by the first ready/error event
    client.start(on_event, state.environment)
    client.request("hello", {}, function(ok, payload)
        if ok then
            state.environment = payload.environment
            state.environments = payload.environments or {}
            state.protected = payload.protected ~= false
            state.writes = payload.writes == true
            state.llm = payload.llm == true
            state.database = payload.database
            state.can_login = payload.can_login == true
            state.models = payload.models or {}
            state.model = state.model or payload.default_model
            state.index_age = payload.index_age
            if is_open() then render_header() end
        end
    end)
end

-- ^t: the next environment in the config. A new dex serve owns its tunnel,
-- schema and index; nothing from one environment is shown in the other.
local function switch_environment()
    if #state.environments < 2 then
        state.status = { phase = "ready", message = "only one environment is configured" }
        render_header()
        return
    end
    local position = 1
    for index, name in ipairs(state.environments) do if name == state.environment then position = index end end
    state.environment = state.environments[(position % #state.environments) + 1]
    state.login_attempted = false
    client.stop()
    state.local_hits, state.id_hits, state.hits = {}, {}, {}
    state.home, state.table_result, state.route = nil, nil, nil
    state.searched_key = nil
    state.index_age, state.index_message = nil, nil
    state.status = { phase = "connect", message = "switching to " .. state.environment }
    ensure_server()
    render_results()
    show_home()
    refresh_chrome()
end

local HELP = {
    { "anywhere", {
        { "Tab / S-Tab", "cycle SEARCH · DEEP · ASK ✦ · SQL" }, { "^u", "clear the prompt" }, { "^g", "home overview" },
        { "^t", "next environment from the config" }, { "?", "this list (normal mode)" }, { "Esc Esc / q", "close DEX" },
        { "mouse", "click selects · double-click opens · wheel scrolls" },
    } },
    { "SEARCH / DEEP", {
        { "type", "app URL · any id · names · deep text (live)" }, { "↑↓ ^j ^k", "move through hits" },
        { "Enter", "open the card, or follow the ↪ suggested tab" }, { "^y", "yank table #id" }, { "^r", "rebuild the index" },
    } },
    { "ASK ✦ / SQL", {
        { "Enter", "run (ASK clears the prompt)" }, { "^e", "bring the last question back" }, { "^o", "next model" },
        { "^l", "into the result" }, { "Enter on a row", "open the row's card" },
    } },
    { "card", {
        { "Enter / BS", "open the line's record / back" }, { "e", "edit the field (opens the write gate)" },
        { "y", "yank value" }, { "o", "open URL / S3 object" }, { "f", "all fields" }, { "r", "home" }, { "i /", "back to the prompt" },
    } },
    { "write gate", { { "a", "approve the preview" }, { "type", "<table> <rows> to commit" }, { "q", "cancel" } } },
}

local function show_help()
    state.card_view = "message"
    local lines = {}
    for _, group in ipairs(HELP) do
        table.insert(lines, group[1]:upper())
        for _, key in ipairs(group[2]) do table.insert(lines, string.format("  %-16s %s", key[1], key[2])) end
        table.insert(lines, "")
    end
    state.message = { title = "? keys", hl = "DexSectionFields", body = table.concat(lines, "\n") }
    render_card()
end

-- ── open / close ─────────────────────────────────────────────────────────
local function map_prompt(buf)
    local insert_and_normal = { "i", "n" }
    local options = { buffer = buf, nowait = true, silent = true }
    for _, key in ipairs({ "<Down>", "<C-j>", "<C-n>" }) do vim.keymap.set(insert_and_normal, key, function() move(1) end, options) end
    for _, key in ipairs({ "<Up>", "<C-k>", "<C-p>" }) do vim.keymap.set(insert_and_normal, key, function() move(-1) end, options) end
    vim.keymap.set("n", "j", function() move(1) end, options)
    vim.keymap.set("n", "k", function() move(-1) end, options)
    vim.keymap.set(insert_and_normal, "<CR>", on_prompt_enter, options)
    vim.keymap.set(insert_and_normal, "<C-u>", clear_prompt, options)
    vim.keymap.set(insert_and_normal, "<C-t>", switch_environment, options)
    vim.keymap.set("n", "?", show_help, options)
    vim.keymap.set(insert_and_normal, "<C-g>", go_home, options)
    vim.keymap.set("n", "r", go_home, options)
    vim.keymap.set(insert_and_normal, "<Tab>", function() cycle_mode(1) end, options)
    vim.keymap.set(insert_and_normal, "<S-Tab>", function() cycle_mode(-1) end, options)
    vim.keymap.set(insert_and_normal, "<C-l>", function() focus("card") end, options)
    vim.keymap.set(insert_and_normal, "<C-o>", cycle_model, options)
    vim.keymap.set(insert_and_normal, "<C-e>", function()
        if state.mode == "ask" and state.last_question then set_prompt(state.last_question) end
    end, options)
    vim.keymap.set(insert_and_normal, "<C-y>", function()
        local hit = state.hits[state.selected]
        if hit then _G.copy_to_system_clipboard(hit.table .. " #" .. hit.id) end
    end, options)
    vim.keymap.set(insert_and_normal, "<C-r>", function()
        request("reindex", {}, function() end)
    end, options)
    for _, key in ipairs({ "<Esc>", "q" }) do vim.keymap.set("n", key, M.close, options) end
end

local function map_card(buf)
    local options = { buffer = buf, nowait = true, silent = true }
    vim.keymap.set("n", "<CR>", drill, options)
    vim.keymap.set("n", "<BS>", back, options)
    vim.keymap.set("n", "-", back, options)
    vim.keymap.set("n", "y", yank, options)
    vim.keymap.set("n", "o", open_url, options)
    vim.keymap.set("n", "e", edit_field, options)
    vim.keymap.set("n", "f", function()
        state.show_all_fields = not state.show_all_fields
        render_card()
    end, options)
    for _, key in ipairs({ "i", "a", "/", "<C-h>", "<Tab>" }) do vim.keymap.set("n", key, function() focus("prompt") end, options) end
    for _, key in ipairs({ "r", "<C-g>" }) do vim.keymap.set("n", key, go_home, options) end
    vim.keymap.set("n", "?", show_help, options)
    vim.keymap.set("n", "<C-t>", switch_environment, options)
    for _, key in ipairs({ "q", "<Esc>" }) do vim.keymap.set("n", key, M.close, options) end
end

-- ── mouse ────────────────────────────────────────────────────────────────
local SCROLL_LINES = 3

local function dex_window_name(winid)
    for name, win in pairs(wins) do
        if win == winid then return name end
    end
    return nil
end

local function on_click(double)
    local position = vim.fn.getmousepos()
    local name = dex_window_name(position.winid)
    if not name then
        -- A click outside DEX dismisses it rather than reaching the editor.
        return M.close()
    end
    if name == "results" then
        if position.line >= 1 and position.line <= #state.hits then
            state.selected = position.line
            render_results()
            if double then
                local hit = state.hits[state.selected]
                load_card(hit.table, hit.id, false)
                focus("card")
            else
                schedule_preview()
                focus("prompt")
            end
        else
            focus("prompt")
        end
    elseif name == "card" then
        focus("card")
        if position.line >= 1 then pcall(vim.api.nvim_win_set_cursor, wins.card, { position.line, math.max(position.column - 1, 0) }) end
        if double then drill() end
    elseif name == "prompt" then
        focus("prompt")
        pcall(vim.api.nvim_win_set_cursor, wins.prompt, { 1, math.max(position.column - 1, 0) })
    end
    -- header/footer clicks do nothing
end

local function on_scroll(direction)
    local name = dex_window_name(vim.fn.getmousepos().winid)
    if name == "results" then
        move(direction, true)
    elseif name == "card" then
        vim.api.nvim_win_call(wins.card, function()
            vim.cmd("normal! " .. SCROLL_LINES .. (direction > 0 and "\5" or "\25"))
        end)
    end
    -- scrolling anywhere else is swallowed: the editor behind stays put
end

local function map_mouse(buf)
    local options = { buffer = buf, nowait = true, silent = true }
    local modes = { "n", "i" }
    vim.keymap.set(modes, "<LeftMouse>", function() on_click(false) end, options)
    vim.keymap.set(modes, "<2-LeftMouse>", function() on_click(true) end, options)
    for _, key in ipairs({ "<LeftDrag>", "<LeftRelease>", "<RightMouse>", "<MiddleMouse>" }) do
        vim.keymap.set(modes, key, "<Nop>", options)
    end
    for _, key in ipairs({ "<ScrollWheelDown>", "<S-ScrollWheelDown>" }) do
        vim.keymap.set(modes, key, function() on_scroll(1) end, options)
    end
    for _, key in ipairs({ "<ScrollWheelUp>", "<S-ScrollWheelUp>" }) do
        vim.keymap.set(modes, key, function() on_scroll(-1) end, options)
    end
    for _, key in ipairs({ "<ScrollWheelLeft>", "<ScrollWheelRight>" }) do vim.keymap.set(modes, key, "<Nop>", options) end
end

local function open_windows()
    local layout = geometry()
    for _, name in ipairs({ "header", "results", "card", "footer", "prompt" }) do
        local buf = ensure_buf(name)
        wins[name] = vim.api.nvim_open_win(buf, name == "prompt", window_config(name, layout[name]))
        vim.wo[wins[name]].winhighlight = "NormalFloat:DexNormal,FloatBorder:DexBorder,FloatTitle:DexTitle"
        vim.wo[wins[name]].wrap = false
    end
    vim.wo[wins.prompt].winhighlight = "NormalFloat:DexPromptNormal,FloatBorder:DexBorderActive,FloatTitle:DexTitle"
    vim.wo[wins.card].cursorline = true
    vim.wo[wins.card].winhighlight = "NormalFloat:DexNormal,FloatBorder:DexBorder,FloatTitle:DexTitle,CursorLine:DexSelected"
    vim.bo[bufs.prompt].modifiable = true
    if not vim.b[bufs.prompt].dex_mapped then
        map_prompt(bufs.prompt)
        map_card(bufs.card)
        for _, name in ipairs({ "prompt", "card", "results", "header", "footer" }) do map_mouse(bufs[name]) end
        vim.b[bufs.prompt].dex_mapped = true
        vim.api.nvim_buf_set_extmark(bufs.prompt, vim.api.nvim_create_namespace("dex_prompt"), 0, 0, {
            virt_text = { { "❯ ", "DexBrandText" } }, virt_text_pos = "inline", right_gravity = false,
        })
        vim.api.nvim_create_autocmd({ "TextChangedI", "TextChanged" }, { group = augroup, buffer = bufs.prompt, callback = on_prompt_changed })
        vim.api.nvim_create_autocmd("WinEnter", {
            group = augroup,
            callback = function()
                local current = vim.api.nvim_get_current_win()
                for name, win in pairs(wins) do
                    if win == current and (name == "results" or name == "header" or name == "footer") then
                        vim.schedule(function() focus("prompt") end)
                    elseif win == current and (name == "prompt" or name == "card") and state.focus ~= name then
                        state.focus = name
                        render_footer()
                    end
                end
            end,
        })
    end
    state.focus = "prompt"
end

function M.close()
    if timers.preview then timers.preview:stop() end
    for _, name in ipairs({ "prompt", "results", "card", "header", "footer" }) do
        if valid_win(name) then pcall(vim.api.nvim_win_close, wins[name], true) end
        wins[name] = nil
    end
    close_approval()
    vim.cmd("stopinsert")
end

function M.open()
    look.define_highlights() -- the Ghostty theme may have changed since last open
    ensure_server()
    open_windows()
    refresh_chrome()
    render_results()
    render_card()
    if state.card_view == "home" then load_home() end
    vim.cmd("startinsert!")
end

function M.toggle()
    if is_open() then
        local here = vim.api.nvim_win_get_tabpage(wins.prompt) == vim.api.nvim_get_current_tabpage()
        M.close()
        if here then return end
    end
    M.open()
end

vim.api.nvim_create_autocmd("VimResized", {
    group = augroup,
    callback = function()
        if not is_open() then return end
        local layout = geometry()
        for name, win in pairs(wins) do
            if vim.api.nvim_win_is_valid(win) then vim.api.nvim_win_set_config(win, window_config(name, layout[name])) end
        end
        refresh_chrome()
        render_results()
        render_card()
    end,
})

-- Internals exposed for the headless test (tests/dex_test.lua).
M._state = state
M._wins = wins
M._bufs = bufs

return M
