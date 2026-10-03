// cross-seed configuration
// Docs: https://www.cross-seed.org/docs/basics/options
//
// Secrets come from the container environment (Vaultwarden item: torrent-clients-eva00):
//   CROSS_SEED_TORZNAB_URL  Prowlarr torznab URL for indexer 28 (includes apikey)
//   CROSS_SEED_QBIT_URL     qbittorrent:http://<user>:<url-encoded password>@qbit-9089:9089
module.exports = {
        // --- Torznab (Prowlarr indexer 28) ---
        torznab: [process.env.CROSS_SEED_TORZNAB_URL],

        // No Sonarr/Radarr media-id searching for now
        sonarr: [],
        radarr: [],

        // --- Source of truth: query qBittorrent's contents directly ---
        useClientTorrents: true,
        torrentDir: null,

        // --- Torrent client (qbit-9089) ---
        torrentClients: [process.env.CROSS_SEED_QBIT_URL],

        // --- Injection + hardlinking ---
        action: "inject",
        linkDirs: ["/downloads/cross-seed-links"],
        linkType: "hardlink",
        linkCategory: "cross-seed-link",
        flatLinking: false,
        duplicateCategories: false,
        skipRecheck: true,
        matchMode: "flexible",
        outputDir: null,

        // --- Data-based matching (disabled; using client torrents) ---
        dataDirs: [],
        maxDataDepth: 2,

        // --- Filtering ---
        includeSingleEpisodes: false,
        includeNonVideos: false,
        seasonFromEpisodes: 1,
        fuzzySizeThreshold: 0.02,
        blockList: [],

        // --- Search throttling / history ---
        delay: 30,
        snatchTimeout: "30 seconds",
        searchTimeout: "2 minutes",
        searchLimit: 400,
        excludeOlder: "2 weeks",
        excludeRecentSearch: "3 days",

        // --- Daemon ---
        host: "0.0.0.0",
        port: 2468,
        rssCadence: "30 minutes",
        searchCadence: "1 day",
        notificationWebhookUrls: [],

        // --- Resume behavior ---
        autoResumeMaxDownload: 52428800,
        ignoreNonRelevantFilesToResume: false,
};
