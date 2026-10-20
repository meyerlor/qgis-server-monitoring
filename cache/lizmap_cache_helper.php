<?php
/**
 * Lizmap tile cache helper for the QGIS Server Monitoring Dashboard.
 *
 * Boots a Lizmap Web Client installation exactly like its own console.php and
 * calls Lizmap's classes directly. Nothing is installed into or changed in the
 * Lizmap instance: no config file is written, no module is added.
 *
 * The only deviation from Lizmap's own CLI is --wms-url: it replaces
 * lizmapServices::$wmsServerURL *in memory, for this process only*, so tile
 * generation can be routed to a dedicated QGIS Server pool without touching
 * lizmapConfig.ini.php or the instance's web traffic.
 *
 * MUST run as the Lizmap PHP user (e.g. www-data). Running as root would create
 * root-owned files in Lizmap's temp and cache directories and break the web app.
 *
 * Usage:
 *   php lizmap_cache_helper.php --lizmap=<path to .../lizmap> [--wms-url=<url>] <command> [args]
 *
 * Commands:
 *   env                                        JSON: version, cache backend, WebP support
 *   projects                                   JSON: repositories and their projects
 *   capabilities <repo> <project>              JSON: layer tree, cfg layers, tile matrix sets
 *   seed <repo> <project> <layer> <tms> <zmin> <zmax> [--bbox=x1,y1,x2,y2] [--force]
 *                                              text: Lizmap's verbose seeding output
 *   clean <repo> <project> [<layer>]           text: Lizmap's cache clean output
 *   wmsping <repo> <project>                   JSON: WMS GetCapabilities of the project at the
 *                                              (overridden) wmsServerURL, with Lizmap's MAP path
 *
 * Copyright (c) 2025 meyerlor - MIT License (see LICENSE)
 */

const EXIT_OK = 0;
const EXIT_USAGE = 2;
const EXIT_ERROR = 1;
const EXIT_SEED_FAILED = 3;

ini_set('display_errors', 'stderr');
error_reporting(E_ALL & ~E_DEPRECATED & ~E_USER_DEPRECATED);

// ---------------------------------------------------------------------------
// Argument parsing: --key=value / --flag anywhere, the rest is positional
// ---------------------------------------------------------------------------
$options = array();
$positional = array();
foreach (array_slice($argv, 1) as $arg) {
    if (strncmp($arg, '--', 2) === 0) {
        $eq = strpos($arg, '=');
        if ($eq === false) {
            $options[substr($arg, 2)] = true;
        } else {
            $options[substr($arg, 2, $eq - 2)] = substr($arg, $eq + 1);
        }
    } else {
        $positional[] = $arg;
    }
}
$command = array_shift($positional);
$jsonCommands = array('env', 'projects', 'capabilities', 'wmsping');

function out_json($data)
{
    echo json_encode($data, JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_PARTIAL_OUTPUT_ON_ERROR), PHP_EOL;
}

function fail($message, $code = EXIT_ERROR)
{
    global $command, $jsonCommands;
    if (in_array($command, $jsonCommands, true)) {
        out_json(array('error' => $message));
    } else {
        fwrite(STDOUT, 'Error: '.$message.PHP_EOL);
    }
    exit($code);
}

if (!$command || !in_array($command, array('env', 'projects', 'capabilities', 'seed', 'clean', 'wmsping'), true)) {
    fwrite(STDERR, "usage: php lizmap_cache_helper.php --lizmap=<dir> [--wms-url=<url>] env|projects|capabilities|seed|clean|wmsping ...\n");
    exit(EXIT_USAGE);
}

if (function_exists('posix_geteuid') && posix_geteuid() === 0) {
    fail('refusing to run as root: run as the Lizmap PHP user (e.g. www-data), '
        .'otherwise root-owned files end up in Lizmap\'s temp and cache directories');
}

$lizmapDir = isset($options['lizmap']) && is_string($options['lizmap']) ? realpath($options['lizmap']) : false;
if (!$lizmapDir || !is_file($lizmapDir.'/application.init.php')) {
    fail('--lizmap must point to the "lizmap" directory of a Lizmap Web Client install (containing application.init.php)', EXIT_USAGE);
}

$wmsUrlOverride = null;
if (isset($options['wms-url'])) {
    $u = (string) $options['wms-url'];
    if (!preg_match('#^https?://[^\s]+$#i', $u)) {
        fail('--wms-url must be an http(s) URL', EXIT_USAGE);
    }
    $wmsUrlOverride = $u;
}

// ---------------------------------------------------------------------------
// Bootstrap: identical to Jelix\Scripts\ModulesCommands::run() (console.php)
// ---------------------------------------------------------------------------
try {
    chdir($lizmapDir);

    require $lizmapDir.'/application.init.php';

    \Jelix\Scripts\Utils::checkEnv();
    \jApp::setEnv('console');
    \Jelix\Scripts\Utils::checkTempPath();

    $fmkInfos = \Jelix\Core\Infos\FrameworkInfos::load();
    $ep = $fmkInfos->getDefaultEntryPointInfo();
    \jApp::setConfig(\jConfigCompiler::read($ep->getConfigFile(), true, true, 'console.php'));
    \jApp::setCoord(new \jCoordinator());
    \jFile::createDir(\jApp::tempPath(), \jApp::config()->chmodDir);

    // Same fake HTTP request the wmts:* commands set up (needed for URL building).
    // jClassicRequest is not autoloadable; www/index.php requires it explicitly too.
    if (!class_exists('jClassicRequest', false)) {
        require_once JELIX_LIB_CORE_PATH.'request/jClassicRequest.class.php';
    }
    $req = new \jClassicRequest();
    $fakeServer = new \Jelix\FakeServerConf\ApacheMod(\jApp::wwwPath(), '/index.php');
    $fakeServer->setHttpRequest($req->getServerURI());

    if (!class_exists('\Lizmap\CliHelpers\WMTSCache') || !class_exists('lizmapTiler')
        || !class_exists('\Lizmap\App\JelixContext')) {
        fail('unsupported Lizmap Web Client version (needs 3.8 or newer)');
    }
} catch (\Throwable $e) {
    fail('Lizmap bootstrap failed: '.$e->getMessage());
}

// ---------------------------------------------------------------------------
// Seeding identity
//
// From the command line nobody is logged in, so Lizmap sends its QGIS Server
// requests with empty Lizmap_User / Lizmap_User_Groups — and the Lizmap plugin
// in QGIS Server rejects every project restricted to groups (this also breaks
// Lizmap's own `wmts:cache:seed` for such projects). This context makes the
// requests of THIS process come from a technical user that is a member of
// exactly the groups the project's .cfg grants access to (options.acl) or
// shows layers to (layers.*.group_visibility). Nothing is changed in Lizmap:
// no user is created, no right is granted.
//
// Tiles therefore look the way an authorised user sees them — Lizmap shares
// cached tiles between all authorised users anyway. Layers filtered by login
// and editable layers are never cached: Lizmap itself forces cached=False for
// them when it loads a project. Projects without groups stay anonymous.
// ---------------------------------------------------------------------------
class LizmapCacheSeederContext extends \Lizmap\App\JelixContext
{
    const SEEDER_LOGIN = 'lizmap-cache-seeder';

    /** @var string[] groups of the technical user; empty = behave like the normal (anonymous) context */
    public $seederGroups = array();

    private function seeding()
    {
        return !empty($this->seederGroups);
    }

    public function UserIsConnected()
    {
        return $this->seeding() ? true : parent::UserIsConnected();
    }

    public function getUserSession()
    {
        if (!$this->seeding()) {
            return parent::getUserSession();
        }

        return (object) array('login' => self::SEEDER_LOGIN, 'email' => '', 'firstname' => '', 'lastname' => '');
    }

    public function aclUserGroupsId()
    {
        return $this->seeding() ? $this->seederGroups : parent::aclUserGroupsId();
    }

    public function aclUserPublicGroupsId($login = null)
    {
        if ($this->seeding() && ($login === null || $login === '')) {
            return $this->seederGroups;
        }

        return parent::aclUserPublicGroupsId($login);
    }

    public function aclCheck($right, $resource = null)
    {
        if ($this->seeding()) {
            if ($right === 'lizmap.repositories.view') {
                return true;   // read access, needed to render the project at all
            }
            if ($right === 'lizmap.tools.loginFilteredLayers.override') {
                return false;  // never lift "filter by login"
            }
        }

        return parent::aclCheck($right, $resource);
    }
}

try {
    // Must be in place before Lizmap creates its services, repositories or
    // projects: they keep a reference to the context they were built with.
    $seederContext = new LizmapCacheSeederContext();
    $rp = new \ReflectionProperty('lizmap', 'appContext');
    $rp->setAccessible(true);
    $rp->setValue(null, $seederContext);

    $services = \lizmap::getServices();
    $configuredWmsUrl = $services->wmsServerURL;
    if ($wmsUrlOverride !== null) {
        // In-memory only: every OGC request of this process reads it at request time
        $services->wmsServerURL = $wmsUrlOverride;
    }
} catch (\Throwable $e) {
    fail('Lizmap bootstrap failed: '.$e->getMessage());
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function services_prop($services, $name, $default = null)
{
    return property_exists($services, $name) ? $services->{$name} : $default;
}

function effective_cache_root($services)
{
    $type = services_prop($services, 'cacheStorageType', 'file');
    $root = services_prop($services, 'cacheRootDirectory', '');
    if ($type == 'redis') {
        return array($root, false);
    }
    // Lizmap silently falls back to the system temp dir (Proxy::createVirtualProfile)
    if (!is_dir($root) || !is_writable($root)) {
        return array(rtrim(sys_get_temp_dir(), '/'), true);
    }

    return array(rtrim(realpath($root), '/'), false);
}

function lizmap_version()
{
    try {
        if (class_exists('\Jelix\Core\Infos\AppInfos')) {
            return \Jelix\Core\Infos\AppInfos::load()->version;
        }
    } catch (\Throwable $e) {
    }

    return null;
}

/**
 * The WebP tile-cache fix (3liz/lizmap-web-client#7232) encodes cropped
 * metatiles with imagewebp(); before it, WebP tiles were stored as JPEG.
 */
function has_webp_cache_fix()
{
    try {
        $file = (new \ReflectionClass('\Lizmap\Request\WMSRequest'))->getFileName();

        return $file && strpos((string) file_get_contents($file), 'imagewebp') !== false;
    } catch (\Throwable $e) {
        return false;
    }
}

function load_project($repoKey, $projectKey)
{
    global $seederContext;
    // groups first: the project must be loaded as the technical user
    $seederContext->seederGroups = project_seed_groups($repoKey, $projectKey);
    $project = \lizmap::getProject($repoKey.'~'.$projectKey);
    if (!$project) {
        throw new \Exception('project not found: '.$repoKey.'~'.$projectKey);
    }

    return $project;
}

function as_list($value)
{
    if (is_string($value)) {
        $value = explode(',', $value);
    }
    if (!is_array($value)) {
        return array();
    }

    return array_values(array_filter(array_map(function ($g) { return trim((string) $g); }, $value), 'strlen'));
}

/**
 * Groups the technical seeding user needs for a project, read straight from
 * its .cfg (before Lizmap loads the project): options.acl + every
 * layers.*.group_visibility. Empty for public projects.
 */
function project_seed_groups($repoKey, $projectKey)
{
    $repo = \lizmap::getRepository($repoKey);
    if (!$repo) {
        throw new \Exception('unknown repository: '.$repoKey);
    }
    $cfg = json_decode((string) @file_get_contents($repo->getPath().$projectKey.'.qgs.cfg'));
    if (!is_object($cfg)) {
        return array();
    }
    $groups = array();
    if (isset($cfg->options) && property_exists($cfg->options, 'acl')) {
        $groups = array_merge($groups, as_list($cfg->options->acl));
    }
    if (isset($cfg->layers)) {
        foreach ($cfg->layers as $layer) {
            if (is_object($layer) && property_exists($layer, 'group_visibility')) {
                $groups = array_merge($groups, as_list($layer->group_visibility));
            }
        }
    }
    // never pretend to be in a user's private group
    $groups = array_filter(array_unique($groups), function ($g) { return strncmp($g, '__priv_', 7) !== 0; });

    return array_values($groups);
}

/**
 * GetCapabilities of a project, sent like Lizmap sends it: same MAP path and
 * the Lizmap_User / Lizmap_User_Groups of this process. Never cached, no redirects.
 */
function qgis_probe($project, $services)
{
    global $seederContext;
    $params = array('MAP' => $project->getRelativeQgisPath(), 'SERVICE' => 'WMS', 'REQUEST' => 'GetCapabilities', 'VERSION' => '1.3.0');
    if ($seederContext->UserIsConnected()) {
        $params['Lizmap_User'] = $seederContext->getUserSession()->login;
        $params['Lizmap_User_Groups'] = implode(', ', $seederContext->aclUserPublicGroupsId());
    } else {
        $params['Lizmap_User'] = '';
        $params['Lizmap_User_Groups'] = '';
    }
    $base = $services->wmsServerURL;
    $sep = strpos($base, '?') === false ? '?' : (substr($base, -1) === '&' ? '' : '&');
    $ctx = stream_context_create(array('http' => array('timeout' => 20, 'ignore_errors' => true, 'follow_location' => 0)));
    $t0 = microtime(true);
    $body = @file_get_contents($base.$sep.http_build_query($params), false, $ctx);
    $ms = (int) round((microtime(true) - $t0) * 1000);
    $code = 0;
    if (isset($http_response_header[0]) && preg_match('#\s(\d{3})(\s|$)#', $http_response_header[0], $m)) {
        $code = (int) $m[1];
    }
    $err = null;
    if ($body === false) {
        $last = error_get_last();
        $err = $last ? $last['message'] : 'request failed';
    }
    $exception = null;
    if (is_string($body) && preg_match('#<ServiceException[^>]*>(.*?)</ServiceException>#si', $body, $m)) {
        $exception = trim(preg_replace('/\s+/', ' ', strip_tags($m[1])));
    }

    return array(
        'wms_url' => $base,
        'map' => $params['MAP'],
        'code' => $code,
        'ms' => $ms,
        'ok' => $body !== false && $code > 0 && $code < 400 && strpos($body, 'WMS_Capabilities') !== false,
        'service_exception' => $exception,
        'snippet' => $body === false ? null : substr(trim(preg_replace('/\s+/', ' ', strip_tags($body))), 0, 200),
        'request_error' => $err,
    );
}

function probe_message($p)
{
    if ($p['request_error']) {
        return 'QGIS Server not reachable at '.$p['wms_url'].': '.$p['request_error'];
    }
    if ($p['service_exception']) {
        return 'QGIS Server answered HTTP '.$p['code'].': '.$p['service_exception'];
    }
    if (!$p['ok']) {
        return 'QGIS Server answered HTTP '.$p['code'].' without capabilities'.($p['snippet'] ? ': '.$p['snippet'] : '');
    }

    return null;
}

/** Walk <layer-tree-group> recursively: keeps QGIS order and grouping. */
function layer_tree_node($xmlNode)
{
    $children = array();
    foreach ($xmlNode->children() as $child) {
        $tag = $child->getName();
        if ($tag === 'layer-tree-group') {
            $children[] = array(
                'type' => 'group',
                'name' => (string) $child['name'],
                'children' => layer_tree_node($child),
            );
        } elseif ($tag === 'layer-tree-layer') {
            $children[] = array(
                'type' => 'layer',
                'name' => (string) $child['name'],
                'id' => (string) $child['id'],
            );
        }
    }

    return $children;
}

function read_layer_tree($qgsPath)
{
    $prev = libxml_use_internal_errors(true);
    $xml = simplexml_load_file($qgsPath, 'SimpleXMLElement', LIBXML_NONET | LIBXML_COMPACT | LIBXML_PARSEHUGE);
    libxml_use_internal_errors($prev);
    if ($xml === false || !isset($xml->{'layer-tree-group'})) {
        return array();
    }

    return layer_tree_node($xml->{'layer-tree-group'});
}

/** Why a cfg layer can or cannot be seeded — mirrors lizmapTiler::getLayerTileInfoList(). */
function seed_status($cfgLayer)
{
    if (strtolower((string) $cfgLayer->name) == 'overview') {
        return 'overview';
    }
    if (!property_exists($cfgLayer, 'cached') || $cfgLayer->cached != 'True') {
        return 'not_cached';
    }
    if (property_exists($cfgLayer, 'singleTile') && $cfgLayer->singleTile == 'True') {
        return 'single_tile';
    }

    return 'ok';
}

function cfg_prop($obj, $name, $default = null)
{
    return property_exists($obj, $name) ? $obj->{$name} : $default;
}

// ---------------------------------------------------------------------------
// Commands
// ---------------------------------------------------------------------------
try {
    switch ($command) {
        case 'env':
            list($cacheRoot, $fallback) = effective_cache_root($services);
            $gd = function_exists('gd_info') ? gd_info() : array();
            out_json(array(
                'lizmap_version' => lizmap_version(),
                'php_version' => PHP_VERSION,
                'lizmap_dir' => $lizmapDir,
                'wms_server_url' => $configuredWmsUrl,
                'wms_server_url_effective' => $services->wmsServerURL,
                'cache_storage_type' => services_prop($services, 'cacheStorageType', 'file'),
                'cache_root_directory' => $cacheRoot,
                'cache_root_fallback_to_tmp' => $fallback,
                'cache_expiration_default' => (int) services_prop($services, 'cacheExpiration', 0),
                'gd' => !empty($gd),
                'gd_webp' => !empty($gd['WebP Support']),
                'webp_cache_fix' => has_webp_cache_fix(),
                'repositories' => count(\lizmap::getRepositoryList()),
            ));

            break;

        case 'projects':
            // Own scan instead of Repository::getProjectsMainData(): that one drops
            // every project the (non-existent) CLI user may not access.
            $result = array();
            foreach (\lizmap::getRepositoryList() as $repoKey) {
                $repo = \lizmap::getRepository($repoKey);
                if (!$repo) {
                    continue;
                }
                $dir = $repo->getPath();
                $projects = array();
                if ($dir && is_dir($dir)) {
                    foreach (scandir($dir) as $file) {
                        if (substr($file, -4) === '.qgs' && is_file($dir.$file.'.cfg')) {
                            $projects[] = array(
                                'key' => substr($file, 0, -4),
                                'qgs_path' => $dir.$file,
                                'qgs_mtime' => filemtime($dir.$file),
                                'cfg_mtime' => filemtime($dir.$file.'.cfg'),
                            );
                        }
                    }
                }
                $result[] = array(
                    'key' => $repoKey,
                    'label' => $repo->getLabel(),
                    'path' => $dir,
                    'projects' => $projects,
                );
            }
            out_json(array('repositories' => $result));

            break;

        case 'capabilities':
            if (count($positional) < 2) {
                fail('usage: capabilities <repo> <project>', EXIT_USAGE);
            }
            list($repoKey, $projectKey) = $positional;
            $project = load_project($repoKey, $projectKey);

            // Configured layers from the .cfg, with the reason a layer cannot be seeded
            $cfgLayers = array();
            foreach ($project->getLayers() as $l) {
                $cfgLayers[] = array(
                    'name' => (string) cfg_prop($l, 'name', ''),
                    'id' => (string) cfg_prop($l, 'id', ''),
                    'type' => (string) cfg_prop($l, 'type', 'layer'),
                    'title' => (string) cfg_prop($l, 'title', ''),
                    'shortname' => cfg_prop($l, 'shortname'),
                    'cached' => cfg_prop($l, 'cached') == 'True',
                    'single_tile' => cfg_prop($l, 'singleTile') == 'True',
                    'image_format' => cfg_prop($l, 'imageFormat'),
                    'cache_expiration' => cfg_prop($l, 'cacheExpiration') !== null ? (int) cfg_prop($l, 'cacheExpiration') : null,
                    'metatile_size' => cfg_prop($l, 'metatileSize'),
                    'client_cache_expiration' => cfg_prop($l, 'clientCacheExpiration'),
                    'seed_status' => seed_status($l),
                );
            }

            // Tile matrix sets + per-layer tile limits, as Lizmap computes them
            $tileCaps = \lizmapTiler::getTileCapabilities($project);
            $tmsList = array();
            $tileLayers = array();
            $capsError = null;
            if ($tileCaps === null || $tileCaps->tileMatrixSetList === null) {
                // Lizmap only logs the reason; ask QGIS Server ourselves to report it
                $capsError = probe_message(qgis_probe($project, $services))
                    ?: 'tile capabilities unavailable (QGIS Server GetCapabilities failed)';
            } else {
                foreach ($tileCaps->tileMatrixSetList as $tms) {
                    $levels = array();
                    foreach ($tms->tileMatrixList as $i => $tm) {
                        $levels[] = array(
                            'id' => $i,
                            'scale_denominator' => $tm->scaleDenominator,
                            'resolution' => $tm->resolution,
                            'left' => $tm->left,
                            'top' => $tm->top,
                        );
                    }
                    $tmsList[] = array('ref' => $tms->ref, 'unit' => $tms->unit, 'levels' => $levels);
                }
                foreach ($tileCaps->layerTileInfoList as $tl) {
                    $links = array();
                    foreach ($tl->tileMatrixSetLinkList as $link) {
                        $limits = array();
                        foreach ($link->tileMatrixLimits as $lim) {
                            $limits[] = array(
                                'id' => (int) $lim->id,
                                'min_row' => (int) $lim->minRow,
                                'max_row' => (int) $lim->maxRow,
                                'min_col' => (int) $lim->minCol,
                                'max_col' => (int) $lim->maxCol,
                                'tiles' => ((int) $lim->maxRow - (int) $lim->minRow + 1) * ((int) $lim->maxCol - (int) $lim->minCol + 1),
                            );
                        }
                        $links[] = array('ref' => $link->ref, 'limits' => $limits);
                    }
                    $tileLayers[] = array(
                        'name' => $tl->name,        // WMTS/WMS name (shortname if set)
                        'id' => $tl->id,
                        'title' => $tl->title,
                        'image_format' => $tl->imageFormat,
                        // Lizmap's file cache directory name for this layer
                        'cache_dir' => sha1($tl->name),
                        'tile_matrix_sets' => $links,
                    );
                }
            }

            $projection = $project->getOption('projection');
            out_json(array(
                'repository' => $repoKey,
                'project' => $projectKey,
                'title' => $project->getTitle(),
                'qgs_path' => $project->getQgisPath(),
                'projection' => is_object($projection) && property_exists($projection, 'ref') ? $projection->ref : null,
                'layer_tree' => read_layer_tree($project->getQgisPath()),
                'cfg_layers' => $cfgLayers,
                'tile_matrix_sets' => $tmsList,
                'tile_layers' => $tileLayers,
                'seed_groups' => $seederContext->seederGroups,
                'error' => $capsError,
            ));

            break;

        case 'seed':
            if (count($positional) < 6) {
                fail('usage: seed <repo> <project> <layer> <tms> <zmin> <zmax> [--bbox=x1,y1,x2,y2] [--force]', EXIT_USAGE);
            }
            list($repoKey, $projectKey, $layerName, $tms, $zmin, $zmax) = $positional;
            $zmin = filter_var($zmin, FILTER_VALIDATE_INT);
            $zmax = filter_var($zmax, FILTER_VALIDATE_INT);
            if ($zmin === false || $zmax === false || $zmax < $zmin) {
                fail('zmin/zmax must be integers with zmin <= zmax', EXIT_USAGE);
            }
            $bbox = null;
            if (isset($options['bbox']) && $options['bbox'] !== true) {
                $bbox = (string) $options['bbox'];
            }
            // WMTSCache loads the project itself: the identity has to be set before
            $seederContext->seederGroups = project_seed_groups($repoKey, $projectKey);
            fwrite(STDOUT, ($seederContext->seederGroups
                ? 'Seeding as technical user '.LizmapCacheSeederContext::SEEDER_LOGIN.' with groups: '.implode(', ', $seederContext->seederGroups)
                : 'Seeding anonymously (public project, no groups configured)').PHP_EOL);
            fflush(STDOUT);
            // One layer per call on purpose: WMTSCache::seed() returns after the
            // first layer when given a list.
            $cache = new \Lizmap\CliHelpers\WMTSCache(function ($line) {
                fwrite(STDOUT, $line.PHP_EOL);
                fflush(STDOUT);
            });
            $rc = $cache->seed($repoKey, $projectKey, $layerName, $tms, $zmin, $zmax, $bbox, true, false, !empty($options['force']));
            exit($rc ? EXIT_SEED_FAILED : EXIT_OK);

        case 'wmsping':
            // Does the (pool) QGIS Server serve this instance's projects? Same MAP
            // path and user/groups Lizmap sends, no cache, no redirects.
            if (count($positional) < 2) {
                fail('usage: wmsping <repo> <project>', EXIT_USAGE);
            }
            $project = load_project($positional[0], $positional[1]);
            $probe = qgis_probe($project, $services);
            $probe['seed_groups'] = $seederContext->seederGroups;
            $probe['message'] = probe_message($probe);
            out_json($probe);

            break;

        case 'clean':
            if (count($positional) < 2) {
                fail('usage: clean <repo> <project> [<layer>]', EXIT_USAGE);
            }
            // clean() reads the tile capabilities, which needs the identity too
            $seederContext->seederGroups = project_seed_groups($positional[0], $positional[1]);
            $cache = new \Lizmap\CliHelpers\WMTSCache(function ($line) {
                fwrite(STDOUT, $line.PHP_EOL);
                fflush(STDOUT);
            });
            $rc = $cache->clean($positional[0], $positional[1], isset($positional[2]) ? $positional[2] : null);
            exit($rc ? EXIT_ERROR : EXIT_OK);
    }
} catch (\Throwable $e) {
    // e.g. DivisionByZeroError inside WMTSCache::seed() when a level has 0 tiles
    fail(get_class($e).': '.$e->getMessage());
}
exit(EXIT_OK);
