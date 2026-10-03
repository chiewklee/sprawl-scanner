package com.mycompany.sprawl;

import java.util.Locale;
import java.util.regex.Pattern;

/** One thing an asset can do: an API operation, an MCP tool or an agent skill. */
public final class Cap {
    public final String asset;      // display name of the owning asset
    public final String assetKey;   // stable key for /assets/{assetId}
    public final String type;       // api | mcp | agent
    public final String name;
    public final String description;
    public final String source;

    public Cap(String asset, String assetKey, String type, String name, String description, String source) {
        this.asset = asset;
        this.assetKey = assetKey;
        this.type = type;
        this.name = name == null ? "" : name;
        this.description = description == null ? "" : description.strip();
        this.source = source;
    }

    public String id() {
        return type + ":" + asset + ":" + name;
    }

    private static final Pattern CAMEL = Pattern.compile("([a-z0-9])([A-Z])");
    private static final Pattern SEPARATORS = Pattern.compile("[_\\-/{}.:]+");
    private static final Pattern SPACES = Pattern.compile("\\s+");
    private static final Pattern PLACEHOLDER_ID =
            Pattern.compile("^(null|undefined|none|operation)?[_\\-]?\\d*$", Pattern.CASE_INSENSITIVE);

    /** 'getAccountById' / 'get_account_by_id' / 'GET /accounts/{id}' -> 'get account by id'. */
    public static String humanize(String s) {
        String out = CAMEL.matcher(s).replaceAll("$1 $2");
        out = SEPARATORS.matcher(out).replaceAll(" ");
        return SPACES.matcher(out).replaceAll(" ").strip().toLowerCase(Locale.ROOT);
    }

    /** operationId, unless it's a generator placeholder like 'null_7': then 'METHOD /path'. */
    public static String operationName(String operationId, String method, String route) {
        String id = operationId == null ? "" : operationId.strip();
        return !id.isEmpty() && !PLACEHOLDER_ID.matcher(id).matches()
                ? id : method.toUpperCase(Locale.ROOT) + " " + route;
    }

    /** No real description (e.g. a bare 'POST /entries'): too little signal to call it a
     *  duplicate, so it can be at most gray. */
    public boolean thin() {
        String d = humanize(description);
        return (d.isEmpty() ? 0 : d.split(" ").length) < 4 || d.equals(humanize(name));
    }

    /** Text used for embedding: humanised name plus description. */
    public String text() {
        return (humanize(name) + ". " + description).strip();
    }
}
