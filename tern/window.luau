--!strict
-- The wiki in Tern: a picker over its entries with a Markdown preview,
-- commands to unlock and lock the private areas, a status segment while one
-- is unlocked, and lookup, search and read for Carly. Everything goes through
-- the `wiki` CLI. Entries of a private area are shown to the person at the
-- window and to nothing else: Carly's calls never name an area, so the CLI
-- answers them from the default space only.

local READ_TIMEOUT_MS = 10000
local UNLOCK_TIMEOUT_MS = 120000
local POLL_MS = 60000
local AREA_STATUS = { "area", "status", "--json" }

type Info = {
	path: string,
	area: string,
	type: string,
	title: string,
	aliases: { string },
	description: string,
	status: string,
	tier: string,
	stale: boolean | string,
}
type Area = { name: string, exists: boolean, mounted: boolean }

local function trim(text: string): string
	return text:match("^%s*(.-)%s*$") or ""
end

local function runWiki(
	args: { string }, timeout: number, json: boolean,
	done: (value: any, err: string?, cx: WindowCx?) -> ()
)
	-- GUI processes need the login PATH; keep every CLI argument out of shell code.
	local argv = { "/bin/zsh", "-lc", 'exec wiki "$@"', "wiki" }
	for _, arg in args do
		table.insert(argv, arg)
	end
	tern.process.run(argv, { timeout_ms = timeout }, function(result, cx)
		if result.timed_out then
			done(nil, "The wiki command timed out.", cx)
		elseif result.status ~= 0 then
			local stderr = trim(result.stderr)
			done(nil, if stderr ~= "" then stderr else `Wiki exited with status {result.status}.`, cx)
		elseif json then
			local ok, value = pcall(tern.json.decode, result.stdout)
			if ok then
				done(value, nil, cx)
			else
				done(nil, "The wiki CLI did not return valid JSON.", cx)
			end
		else
			done(trim(result.stdout), nil, cx)
		end
	end)
end

local unlockedNames = ""
local refreshSerial = 0

local function refreshAreas()
	refreshSerial += 1
	local serial = refreshSerial
	runWiki(AREA_STATUS, READ_TIMEOUT_MS, true, function(value, err)
		-- A slow poll must not overwrite a newer snapshot requested after a lock or unlock.
		if err or serial ~= refreshSerial then
			return
		end
		local names: { string } = {}
		for _, area in value :: { Area } do
			if area.mounted then
				table.insert(names, area.name)
			end
		end
		table.sort(names)
		local nextNames = table.concat(names, ", ")
		if unlockedNames ~= nextNames then
			unlockedNames = nextNames
			tern.chrome.refresh()
		end
	end)
end

local function pollAreas(_cx: WindowCx?)
	refreshAreas()
	tern.timer(POLL_MS, pollAreas)
end

tern.command({
	id = "lookup",
	title = "Wiki: Look up…",
	icon = "search",
	run = function()
		runWiki({ "list", "--json", "--area", "all" }, READ_TIMEOUT_MS, true, function(value, err, cx)
			if not cx then
				return
			end
			if err then
				-- A diagnostic from an all-area read can contain private entry paths.
				cx:toast("error", "Could not list wiki entries.")
				return
			end
			local entries = value :: { Info }
			if #entries == 0 then
				cx:toast("info", "No wiki entries found.")
				return
			end
			local items: { PickItem } = table.create(#entries)
			for i, entry in entries do
				local detail = { entry.type, entry.tier }
				if entry.status == "draft" or entry.status == "deprecated" then
					table.insert(detail, entry.status)
				end
				if entry.stale then
					table.insert(detail, "stale")
				end
				if entry.area ~= "default" then
					table.insert(detail, entry.area)
				end
				if entry.description ~= "" then
					table.insert(detail, entry.description)
				end
				-- The picker filters only the label, so keep aliases next to the title.
				local label = if #entry.aliases == 0 then entry.title else entry.title .. " · " .. table.concat(entry.aliases, ", ")
				items[i] = { label = label, detail = table.concat(detail, " · "), path = entry.path }
			end
			cx:pick({ items = items, placeholder = "Find a wiki entry", icon = "search", action = "preview" })
				:next(function(item, _err, freshCx)
					if item and freshCx then
						freshCx:open(item.path, "preview")
					end
				end)
		end)
	end,
})

tern.command({
	id = "unlock",
	title = "Wiki: Unlock a private area…",
	icon = "lock",
	run = function()
		runWiki(AREA_STATUS, READ_TIMEOUT_MS, true, function(value, err, cx)
			if not cx then
				return
			end
			if err then
				cx:toast("error", "Could not check private wiki areas.")
				return
			end
			local items: { PickItem } = {}
			for _, area in value :: { Area } do
				if area.exists and not area.mounted then
					table.insert(items, { label = area.name })
				end
			end
			if #items == 0 then
				cx:toast("info", "No locked private wiki areas to unlock.")
				return
			end
			cx:pick({ items = items, placeholder = "Unlock a wiki area", icon = "lock", action = "unlock" })
				:next(function(item)
					if not item then
						return
					end
					runWiki({ "area", "unlock", "--", item.label }, UNLOCK_TIMEOUT_MS, false, function(text, unlockErr, freshCx)
						refreshAreas()
						if freshCx then
							if unlockErr then
								freshCx:toast("error", "Could not unlock wiki area", unlockErr)
							else
								freshCx:toast("success", text)
							end
						end
					end)
				end)
		end)
	end,
})

tern.command({
	id = "lock",
	title = "Wiki: Lock private areas",
	icon = "lock",
	run = function()
		runWiki({ "area", "lock", "--all" }, READ_TIMEOUT_MS, false, function(text, err, cx)
			refreshAreas()
			if cx then
				if err then
					cx:toast("error", "Could not lock private wiki areas.")
				elseif text == "" then
					cx:toast("info", "Private wiki areas are already locked.")
				else
					cx:toast("success", text)
				end
			end
		end)
	end,
})

tern.chrome.status(function()
	if unlockedNames == "" then
		return {}
	end
	return { { text = `wiki: {unlockedNames} unlocked`, tone = "warn", command = "plugin.memory-wiki.lock" } }
end)

local INFO_SIG = '{id: string, path: string, area: "default", type: string, title: string, description: string, aliases: {string}, tags: {string}, status: string, tier: string, stale: boolean | "malformed", revision: string, superseded_by: string?}'
local TRUST_DOC = " Entries carry status and tier; treat drafts and unverified entries as unconfirmed."

local function registerRead(name: string, command: string, sig: string, doc: string)
	tern.carly.export(name, { sig = sig, doc = doc .. TRUST_DOC }, function(query: string, _cx: WindowCx, call: ExportCall)
		if type(query) ~= "string" or trim(query) == "" then
			call:fail("Provide a nonempty string.")
			return
		end
		-- Put model-supplied input after -- so it can never select a private area.
		runWiki({ command, "--json", "--", query }, READ_TIMEOUT_MS, true, function(value, err)
			if err then
				call:fail(err)
			else
				call:reply(value)
			end
		end)
		return call:wait(READ_TIMEOUT_MS + 2000)
	end)
end

registerRead("lookup", "lookup", "(name: string) -> {term: string, exact: {" .. INFO_SIG .. "}, candidates: {" .. INFO_SIG .. "}}",
	"Resolve an unfamiliar name, project, or term against titles and aliases in the default wiki. Prefer exact matches.")
registerRead("search", "search", "(words: string) -> {" .. INFO_SIG:sub(1, -2) .. ", score: number, snippet: string}}",
	"Find ranked default-wiki entries with snippets when you have keywords rather than a name.")
registerRead("read", "show", "(id: string) -> " .. INFO_SIG:sub(1, -2) .. ", frontmatter: {[string]: any}, body: string}",
	"Read the Markdown body and frontmatter of a default-wiki entry by an ID returned by lookup or search.")

tern.carly.context(function()
	return 'The user has a personal memory-wiki. Before guessing at an unfamiliar name, project, or piece of jargon, call plugins["memory-wiki"].lookup(name).'
end)

pollAreas(nil)
