-- DEX visual vocabulary: per-table glyphs and colors, text fitting, and a
-- line builder that records highlight ranges as text is composed.
--
-- Colors are cterm indexes. 0-15 come from the Ghostty theme (the same slots
-- the tabline uses), so DEX retints with the terminal; panel shading uses the
-- fixed 256-color grays, after the contrast gate in define_highlights().
local M = {}

M.namespace = vim.api.nvim_create_namespace("dex")

-- glyph, ANSI color slot. Tables not listed hash onto the accent cycle.
local TABLE_STYLES = {
    accounts = { "◆", 13 },
    organizations = { "◆", 13 },
    users = { "●", 6 },
    customers = { "●", 6 },
    products = { "▣", 10 },
    orders = { "■", 9 },
    invoices = { "▤", 3 },
    payments = { "$", 2 },
    projects = { "❖", 13 },
    tasks = { "☐", 14 },
    files = { "▥", 12 },
    images = { "◉", 11 },
    videos = { "▷", 9 },
    tags = { "#", 4 },
    comments = { "✉", 3 },
    messages = { "❝", 5 },
    conversations = { "❝", 5 },
    events = { "⌁", 10 },
    notifications = { "!", 11 },
}
local ACCENT_CYCLE = { 9, 10, 11, 12, 13, 14 }

function M.table_style(table_name)
    local style = TABLE_STYLES[table_name]
    if style then return style[1], "DexC" .. style[2] end
    local hash = 0
    for index = 1, #table_name do hash = (hash * 31 + table_name:byte(index)) % 9973 end
    return "•", "DexC" .. ACCENT_CYCLE[(hash % #ACCENT_CYCLE) + 1]
end

-- Readable singular name for headings: order_items -> ORDER ITEM.
function M.heading_name(table_name)
    local singular = table_name:gsub("ies$", "y"):gsub("ches$", "ch"):gsub("sses$", "ss"):gsub("([^s])s$", "%1")
    return singular:gsub("_", " "):upper()
end

M.SPINNER = { "⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏" }

function M.clean(text)
    if text == nil or text == vim.NIL then return "∅" end
    return (tostring(text):gsub("[%c]", " "))
end

-- Pad or cut to exactly `width` display cells.
function M.fit(text, width)
    text = M.clean(text)
    if width <= 0 then return "" end
    local cells = vim.fn.strdisplaywidth(text)
    if cells <= width then return text .. string.rep(" ", width - cells) end
    local characters = vim.fn.strchars(text)
    local low, high = 0, characters
    while low < high do -- longest prefix that leaves room for the ellipsis
        local middle = math.floor((low + high + 1) / 2)
        if vim.fn.strdisplaywidth(vim.fn.strcharpart(text, 0, middle)) <= width - 1 then low = middle else high = middle - 1 end
    end
    local cut = vim.fn.strcharpart(text, 0, low) .. "…"
    return cut .. string.rep(" ", width - vim.fn.strdisplaywidth(cut))
end

function M.human_age(seconds)
    if seconds == nil or seconds == vim.NIL then return "none" end
    if seconds < 90 then return string.format("%ds", seconds) end
    if seconds < 5400 then return string.format("%dm", seconds / 60) end
    return string.format("%.1fh", seconds / 3600)
end

-- ── line builder ─────────────────────────────────────────────────────────
local Builder = {}
Builder.__index = Builder

function M.builder()
    return setmetatable({ lines = {}, marks = {}, line_data = {} }, Builder)
end

-- chunks: { {text, hl_group_or_nil}, ... }; data is attached to the line
-- (a drillable node, an editable field) and read back by cursor position.
function Builder:line(chunks, data)
    local text = ""
    local row = #self.lines
    for _, chunk in ipairs(chunks) do
        local start = #text
        text = text .. chunk[1]
        if chunk[2] then
            table.insert(self.marks, { row = row, start_col = start, end_col = #text, hl = chunk[2] })
        end
    end
    table.insert(self.lines, text)
    self.line_data[#self.lines] = data
    return #self.lines
end

function Builder:blank() return self:line({ { "" } }) end

-- Highlight a byte range on the most recent line.
function Builder:mark(start_col, end_col, hl)
    table.insert(self.marks, { row = #self.lines - 1, start_col = start_col, end_col = end_col, hl = hl })
end

function Builder:apply(buf)
    vim.bo[buf].modifiable = true
    vim.api.nvim_buf_set_lines(buf, 0, -1, false, self.lines)
    vim.bo[buf].modifiable = false
    vim.api.nvim_buf_clear_namespace(buf, M.namespace, 0, -1)
    for _, mark in ipairs(self.marks) do
        pcall(vim.api.nvim_buf_set_extmark, buf, M.namespace, mark.row, mark.start_col, {
            end_col = mark.end_col, hl_group = mark.hl, priority = 120,
        })
    end
end

-- ── SQL keyword highlighting (cheap, no parser) ──────────────────────────
local SQL_KEYWORDS = {}
for word in ([[select from where join left right inner outer on and or not group by order having
limit as distinct count sum avg min max with union all case when then else end in is null like
between exists asc desc interval update set delete insert into values coalesce cast over partition]]):gmatch("%S+") do
    SQL_KEYWORDS[word] = true
end

function M.sql_marks(builder, line_number, text, offset)
    offset = offset or 0
    for start, word, finish in text:gmatch("()([%a_]+)()") do
        if SQL_KEYWORDS[word:lower()] then
            table.insert(builder.marks, { row = line_number - 1, start_col = offset + start - 1, end_col = offset + finish - 1, hl = "DexSqlKeyword" })
        end
    end
    for start, finish in text:gmatch("()'[^']*'()") do
        table.insert(builder.marks, { row = line_number - 1, start_col = offset + start - 1, end_col = offset + finish - 1, hl = "DexSqlString" })
    end
    for start, finish in text:gmatch("()%f[%w]%d+%f[%W]()") do
        table.insert(builder.marks, { row = line_number - 1, start_col = offset + start - 1, end_col = offset + finish - 1, hl = "DexSqlNumber" })
    end
end

-- ── highlights with a contrast gate ──────────────────────────────────────
-- Accent slots come from the Ghostty theme, whose "bright magenta" may be
-- #801070 on one theme and #ff79c6 on the next. Each slot is checked against
-- the panel background; a slot too dark to read falls back to a light 256
-- color of the same hue, and chip text picks black or white per slot.
local PANEL_BACKGROUND = { 28, 28, 28 } -- cterm 234
local MINIMUM_TEXT_CONTRAST = 4.0
local BLACK, WHITE = 16, 231
local LIGHT_TWIN = { [1] = 210, [2] = 114, [3] = 215, [4] = 111, [5] = 177, [6] = 80,
    [9] = 210, [10] = 156, [11] = 228, [12] = 117, [13] = 213, [14] = 123 }

local function channel(value)
    value = value / 255
    return value <= 0.03928 and value / 12.92 or ((value + 0.055) / 1.055) ^ 2.4
end

local function luminance(rgb)
    return 0.2126 * channel(rgb[1]) + 0.7152 * channel(rgb[2]) + 0.0722 * channel(rgb[3])
end

local function contrast(first, second)
    local a, b = luminance(first), luminance(second)
    if a < b then a, b = b, a end
    return (a + 0.05) / (b + 0.05)
end

local function hex_rgb(hex)
    return { tonumber(hex:sub(2, 3), 16), tonumber(hex:sub(4, 5), 16), tonumber(hex:sub(6, 7), 16) }
end

-- Without a theme module, slots are the xterm defaults, and every 256 index
-- also gets a hex twin so the groups render with termguicolors on.
local XTERM_ANSI = { [0] = "#000000", "#cd0000", "#00cd00", "#cdcd00", "#0000ee", "#cd00cd", "#00cdcd", "#e5e5e5",
    "#7f7f7f", "#ff0000", "#00ff00", "#ffff00", "#5c5cff", "#ff00ff", "#00ffff", "#ffffff" }

local active_slots = XTERM_ANSI

local function cterm_hex(index)
    if type(index) ~= "number" then return nil end
    if index < 16 then return active_slots[index] or XTERM_ANSI[index] end
    if index >= 232 then
        local level = 8 + (index - 232) * 10
        return string.format("#%02x%02x%02x", level, level, level)
    end
    local cube = index - 16
    local function step(value) return value == 0 and 0 or 55 + value * 40 end
    return string.format("#%02x%02x%02x", step(math.floor(cube / 36)), step(math.floor(cube / 6) % 6), step(cube % 6))
end

local function resolve_palette()
    local ok, theme = pcall(require, "theme")
    local slots = (ok and type(theme) == "table" and type(theme.palette) == "function") and theme.palette() or XTERM_ANSI
    active_slots = slots
    local text, chip_text = {}, {}
    for slot = 1, 15 do
        local hex = slots[slot]
        if hex then
            local rgb = hex_rgb(hex)
            local readable = contrast(rgb, PANEL_BACKGROUND) >= MINIMUM_TEXT_CONTRAST
            text[slot] = readable and slot or (LIGHT_TWIN[slot] or 252)
            chip_text[slot] = contrast(rgb, { 0, 0, 0 }) >= contrast(rgb, { 255, 255, 255 }) and BLACK or WHITE
        else
            text[slot], chip_text[slot] = slot, BLACK
        end
    end
    return text, chip_text
end

-- fg/bg: a theme slot as "s<n>" (contrast-gated) or a fixed 256 index.
local GROUPS = {
    DexNormal = { bg = 234, fg = 252 },
    DexPromptNormal = { bg = 235, fg = 255 },
    DexBorder = { bg = 234, fg = 240 },
    DexBorderActive = { bg = 235, fg = "s13" },
    DexTitle = { bg = 234, fg = 255, bold = true },
    DexTitleText = { fg = 255 },
    DexHeading = { fg = 255, bold = true },
    DexBrand = { chip = "s13", bold = true },
    DexBrandText = { fg = "s13", bold = true },
    DexAccent = { fg = "s14" },
    DexKeyword = { fg = "s14" },
    DexChipDanger = { chip = "s9", bold = true },
    DexChipStaging = { chip = "s3", bold = true },
    DexChipSafe = { chip = "s10", bold = true },
    DexChipMuted = { bg = 238, fg = 250 },
    DexChipModel = { chip = "s12", bold = true },
    DexChipBrand = { chip = "s5", bold = true },
    DexError = { fg = "s9" },
    DexDangerText = { fg = "s9", bold = true },
    DexWarn = { fg = "s11", bold = true },
    DexGood = { fg = "s10" },
    DexBusy = { fg = "s11" },
    DexDim = { fg = 245 },
    DexKey = { bg = 237, fg = "s14", bold = true },
    DexKeyDanger = { chip = "s9", bold = true },
    DexModeActive = { chip = "s14", bold = true },
    DexModeIdle = { bg = 235, fg = 245 },
    DexModeSuggested = { chip = "s11", bold = true },
    DexSuggestHint = { fg = "s11", italic = true },
    DexSelected = { bg = 237 },
    DexSelectedBar = { fg = "s13", bold = true },
    DexId = { fg = "s3" },
    DexIdStrong = { fg = "s11", bold = true },
    DexDeleted = { fg = 242, strikethrough = true },
    DexMatch = { fg = "s11", bold = true, underline = true },
    DexBrandTag = { fg = "s5", italic = true },
    DexSectionId = { chip = "s11", bold = true },
    DexSectionFuzzy = { chip = "s14", bold = true },
    DexSectionDeep = { chip = "s12", bold = true },
    DexSectionFields = { bg = 238, fg = 255, bold = true },
    DexSectionParents = { chip = "s13", bold = true },
    DexSectionLineage = { chip = "s10", bold = true },
    DexSectionLinked = { chip = "s12", bold = true },
    DexSectionChildren = { chip = "s14", bold = true },
    DexFieldKey = { fg = 246 },
    DexValue = { fg = 252 },
    DexValueAlt = { fg = 249 },
    DexCount = { fg = "s11", bold = true },
    DexColumn = { fg = 255, bold = true },
    DexColumnLink = { fg = "s14", bold = true, underline = true },
    DexRule = { fg = 240 },
    DexSql = { fg = 252 },
    DexSqlKeyword = { fg = "s13", bold = true },
    DexSqlString = { fg = "s10" },
    DexSqlNumber = { fg = "s9" },
    DexDangerBorder = { bg = 52, fg = "s9", bold = true },
    DexApprovalNormal = { bg = 52, fg = 255 },
}

function M.define_highlights()
    local text, chip_text = resolve_palette()
    local function slot_of(value) return type(value) == "string" and tonumber(value:sub(2)) or nil end
    for slot = 1, 15 do
        vim.api.nvim_set_hl(0, "DexC" .. slot, { ctermfg = text[slot], fg = cterm_hex(text[slot]) })
    end
    for group, spec in pairs(GROUPS) do
        local definition = {
            bold = spec.bold, italic = spec.italic, underline = spec.underline, strikethrough = spec.strikethrough,
        }
        local chip_slot = slot_of(spec.chip)
        if chip_slot then
            definition.ctermbg, definition.ctermfg = chip_slot, chip_text[chip_slot]
        else
            local fg_slot, bg_slot = slot_of(spec.fg), slot_of(spec.bg)
            definition.ctermfg = fg_slot and text[fg_slot] or spec.fg
            definition.ctermbg = bg_slot or spec.bg
        end
        definition.cterm = {
            bold = spec.bold, italic = spec.italic, underline = spec.underline, strikethrough = spec.strikethrough,
        }
        definition.fg, definition.bg = cterm_hex(definition.ctermfg), cterm_hex(definition.ctermbg)
        vim.api.nvim_set_hl(0, group, definition)
    end
end

return M
