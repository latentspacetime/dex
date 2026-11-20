-- DEX database explorer: <Leader>d / <Leader>D and :Dex toggle the float.
-- Keys that the config already maps are left alone; :Dex always works.
if vim.g.loaded_dex then return end
vim.g.loaded_dex = true

-- lua/dex yanks through this global; configs without one get the + register.
if not _G.copy_to_system_clipboard then
    _G.copy_to_system_clipboard = function(text)
        vim.fn.setreg("+", text)
        vim.fn.setreg('"', text)
    end
end

for _, key in ipairs({ "d", "D" }) do
    if vim.fn.maparg("<Leader>" .. key, "n") == "" then
        vim.keymap.set("n", "<Leader>" .. key, function() require("dex").toggle() end,
            { silent = true, desc = "Toggle DEX database explorer" })
    end
end

vim.api.nvim_create_user_command("Dex", function() require("dex").toggle() end, { desc = "Toggle DEX database explorer" })
