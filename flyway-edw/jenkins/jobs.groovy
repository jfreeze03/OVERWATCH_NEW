// =====================================================================================================================
// EDW Flyway kit - Job DSL seed script (jenkins/jobs.groovy).
//
// Run it from an ADMIN-ONLY seed job (docs/GUIDE.md C3): Freestyle job, Source Code Management = the EDW repository,
// branch main; build step "Process Job DSLs" > "Look on Filesystem" > jenkins/jobs.groovy. If Job DSL script
// security is on, approve this script once under In-process Script Approval. The script reads runways.toml from the
// seed job's workspace, so the runway folders and deploy jobs always follow the manifest.
//
// Creates (re-running it is idempotent; it updates what it created and deletes nothing):
//   EDW                         top folder               credential: sf-account (Secret text, account identifier)
//   EDW/ci                      CI folder                credentials: sf-flyway-AUDIT, flyway-license,
//                                                        bitbucket-status-token (also the branch-source scan token)
//   EDW/ci/edw-flyway           multibranch pipeline     Bitbucket Cloud, Script Path jenkins/Jenkinsfile
//   EDW/deploy                  deploy folder            credential: bitbucket-checkout (Jenkinsfile.deploy from main)
//   EDW/deploy/<ID>             one folder per runway    credentials: sf-flyway-<ID>, sf-flyway-AUDIT,
//                                                        flyway-license, bitbucket-status-token
//   EDW/deploy/<ID>/deploy      pipeline job             jenkins/Jenkinsfile.deploy from main, lightweight checkout
//
// Why one folder per runway: folder credentials are visible to that folder's children only, so the PRD key in
// EDW/deploy/PRD can never be resolved by a PR build in EDW/ci or by another runway's job (FACTS I6-02, I6-08).
// The deploy job takes its runway from its folder name (Jenkinsfile.deploy expectedTarget(); an EDW_TARGET
// environment variable, if your folders define one, wins - never name it FLYWAY_TARGET, which Flyway reads as its
// migrate target version) and its TARGET choice is restricted to that one id here and again by the Jenkinsfile's
// own parameters block.
//
// Not done here, on purpose:
// * Credentials: Job DSL would need the secret values in code or in the seed job's environment. Create them per
//   folder in the UI or from your secrets tooling (docs/GUIDE.md B4); every folder description lists its own.
// * Copy-artifact permission: multibranch branch jobs take job properties from their Jenkinsfile, so
//   jenkins/Jenkinsfile sets options { copyArtifactPermission('/EDW/deploy/*') } itself.
// * Permissions: Role Strategy item roles in jenkins/casc-snippet.yaml.
//
// Required plugins: job-dsl, cloudbees-folder, workflow-job, workflow-cps, workflow-multibranch,
// bitbucket-branch-source, git, credentials; the pipelines additionally need lockable-resources, copyartifact,
// pipeline-utility-steps, docker-workflow, credentials-binding, pipeline-input-step, pipeline-build-step, junit,
// timestamper; casc-snippet.yaml needs configuration-as-code and role-strategy.
// =====================================================================================================================

// ---- Settings ---------------------------------------------------------------------------------------------------
// Bitbucket workspace and repository slugs (https://bitbucket.org/<workspace>/<repository>). Leave them empty to use
// the controller-wide environment variables BITBUCKET_WORKSPACE / BITBUCKET_REPO from casc-snippet.yaml, which the
// deploy jobs also use for their commit statuses.
String bitbucketWorkspace = ''
String bitbucketRepo = ''
// Secret text: a Bitbucket repository access token (Repositories: Read, Pull requests: Read; add Webhooks: Read and
// write only if Jenkins manages the webhook). The plugin uses it to scan, check out and post PR build statuses.
String scanCredentialsId = 'bitbucket-status-token'
// Username with password for the deploy jobs' plain git checkout of main: username x-token-auth, password = the same
// repository access token (the git plugin cannot use a Secret text credential). Stored in EDW/deploy.
String checkoutCredentialsId = 'bitbucket-checkout'
// '' when the kit is the repository root (docs/GUIDE.md C1); otherwise its folder, e.g. 'flyway-edw'.
String kitDir = ''
// The multibranch project; jenkins/Jenkinsfile.deploy (ciProject()) and casc-snippet.yaml use the same path.
String ciProjectPath = 'EDW/ci/edw-flyway'

// ---- Resolve settings -------------------------------------------------------------------------------------------
String workspaceSlug = bitbucketWorkspace ?: (binding.hasVariable('BITBUCKET_WORKSPACE')
    ? String.valueOf(binding.getVariable('BITBUCKET_WORKSPACE')).trim() : '')
String repoSlug = bitbucketRepo ?: (binding.hasVariable('BITBUCKET_REPO')
    ? String.valueOf(binding.getVariable('BITBUCKET_REPO')).trim() : '')
if (!(workspaceSlug ==~ /[A-Za-z0-9][A-Za-z0-9._-]*/) || !(repoSlug ==~ /[A-Za-z0-9][A-Za-z0-9._-]*/)) {
    throw new IllegalStateException('jobs.groovy: set bitbucketWorkspace/bitbucketRepo at the top of this file, or the '
        + "BITBUCKET_WORKSPACE/BITBUCKET_REPO environment variables (casc-snippet.yaml); got '${workspaceSlug}' / "
        + "'${repoSlug}'")
}
String prefix = kitDir ? kitDir.replaceAll('/+$', '') + '/' : ''
String repoUrl = "https://bitbucket.org/${workspaceSlug}/${repoSlug}.git"

// ---- Read runways.toml (only the quoted string keys this script needs; fw.py owns full validation, FG019) --------
String runwaysPath = prefix + 'runways.toml'
String runwaysText = readFileFromWorkspace(runwaysPath)
Map<String, String> kit = [:]
Map<String, Map<String, String>> runways = [:]          // LinkedHashMap: keeps the file's DEV..PRD order
String section = ''
runwaysText.eachLine { String raw ->
    String line = raw.trim()
    if (!line || line.startsWith('#')) {
        return
    }
    def header = line =~ /^\[([A-Za-z0-9_.]+)\]$/
    if (header.matches()) {
        section = header.group(1)
        return
    }
    def kv = line =~ /^([A-Za-z0-9_]+)\s*=\s*"([^"]*)"\s*(#.*)?$/
    if (!kv.matches()) {
        return
    }
    if (section == 'kit') {
        kit[kv.group(1)] = kv.group(2)
    } else if (section.startsWith('runways.')) {
        String id = section.substring('runways.'.length())
        if (!runways.containsKey(id)) {
            runways[id] = [:]
        }
        runways[id][kv.group(1)] = kv.group(2)
    }
}
if (runways.isEmpty()) {
    throw new IllegalStateException("jobs.groovy: no [runways.<ID>] tables in ${runwaysPath}: is the seed job "
        + 'checking out the EDW repository?')
}
runways.each { String id, Map<String, String> rw ->
    if (!(id ==~ /[A-Z]{3}/) || !rw.jenkins_folder || !rw.database || !rw.credentials_id) {
        throw new IllegalStateException("jobs.groovy: runways.toml [runways.${id}] needs a three-letter id, "
            + 'jenkins_folder, database and credentials_id (run python3 scripts/fw.py guard)')
    }
}
String mainBranch = kit.main_branch ?: 'main'
String auditCredentialsId = kit.auditor_credentials_id ?: 'sf-flyway-AUDIT'

// ---- Folders ----------------------------------------------------------------------------------------------------
Set<String> madeFolders = [] as Set
Closure ensureFolder = { String path, String text ->
    List<String> parts = path.split('/') as List
    for (int i = 1; i <= parts.size(); i++) {
        String p = parts.subList(0, i).join('/')
        boolean leaf = i == parts.size()
        if (!madeFolders.contains(p) || leaf) {
            folder(p) {
                if (leaf && text) {
                    description(text)
                }
            }
            madeFolders.add(p)
        }
    }
}

ensureFolder('EDW', 'Snowflake EDW schema migrations with Flyway Enterprise (docs/GUIDE.md). '
    + 'Credential here: sf-account (Secret text: the Snowflake account identifier, <orgname>-<account_name>).')
ensureFolder('EDW/ci', 'Continuous integration for the EDW repository. Everything here is reachable by any pull '
    + "request, so it holds only read-only material: ${auditCredentialsId} (SSH Username with private key: the "
    + 'read-only auditor login), flyway-license (Username with password: Redgate email + personal access token), '
    + "${scanCredentialsId} (Secret text: Bitbucket repository access token).")
ensureFolder('EDW/deploy', 'One folder per runway; each holds only its own deploy key. Credential here: '
    + "${checkoutCredentialsId} (Username with password: x-token-auth + the Bitbucket repository access token, used "
    + 'to load jenkins/Jenkinsfile.deploy from main).')

// ---- CI: multibranch pipeline -----------------------------------------------------------------------------------
multibranchPipelineJob(ciProjectPath) {
    displayName('edw-flyway')
    description('Pull-request validation (guard, flyway check -code, info/validate against DEV with the read-only '
        + 'auditor login), immutable bundles from main and plain branches, automatic DEV deploy of every main build. '
        + "Script Path ${prefix}jenkins/Jenkinsfile.")
    branchSources {
        branchSource {
            source {
                bitbucket {
                    // A fixed id keeps branch jobs and their history across seed runs.
                    id('edw-flyway-bitbucket-cloud')
                    serverUrl('https://bitbucket.org')
                    repoOwner(workspaceSlug)
                    repository(repoSlug)
                    credentialsId(scanCredentialsId)
                    traits {
                        // Strategy 1 = branches that are not also PRs: one build and one status key per commit
                        // (FACTS I1-03, I5-02).
                        bitbucketBranchDiscovery {
                            strategyId(1)
                        }
                        // Strategy 1 = MERGE: a PR builds its merge result with the target branch, which is fetched
                        // as origin/<target> for the guard's FG003 immutability check (FACTS I1-02).
                        bitbucketPullRequestDiscovery {
                            strategyId(1)
                        }
                        // No bitbucketForkDiscovery on purpose: forks never run in this Jenkins (FACTS I1-04).
                        // Plugin defaults kept: UNSTABLE, ABORTED and NOT_BUILT all report FAILED, so an aborted or
                        // test-failing PR build cannot pass the merge check (FACTS I1-13, I5-03).
                        bitbucketBuildStatusNotifications {
                            sendSuccessNotificationForUnstableBuild(false)
                            sendStoppedNotificationForAbortBuild(false)
                        }
                    }
                }
            }
        }
    }
    factory {
        workflowBranchProjectFactory {
            scriptPath(prefix + 'jenkins/Jenkinsfile')
        }
    }
    orphanedItemStrategy {
        discardOldItems {
            daysToKeep(14)
            numToKeep(50)
        }
    }
    // Webhooks (repo:push, pullrequest:*) drive builds; a daily scan catches a lost webhook delivery.
    triggers {
        periodicFolderTrigger {
            interval('1d')
        }
    }
}

// ---- Deploy: one folder + one job per runway --------------------------------------------------------------------
runways.each { String id, Map<String, String> rw ->
    String folderPath = rw.jenkins_folder
    String approval = rw.approval_group ? "approval by ${rw.approval_group}" : 'no approval step'
    String deployUser = rw.service_user ?: 'the deploy user'
    ensureFolder(folderPath, "Runway ${id}: ${rw.database} (${rw.tier ?: 'tier ?'}), ${approval}. Credentials in "
        + "this folder only: ${rw.credentials_id} (SSH Username with private key: ${deployUser}, encrypted PKCS#8 "
        + "key + passphrase), ${auditCredentialsId}, flyway-license, ${scanCredentialsId}. "
        + "Lock: ${rw.lock ?: rw.database}.")

    pipelineJob("${folderPath}/deploy") {
        displayName("deploy ${id}")
        description("Deploys a verified bundle to ${rw.database}: policy from runways.toml on main, promotion check, "
            + 'lock, pre-flight, drift, rehearsal (prod tier), approval, Time Travel point, migrate. '
            + "Script Path ${prefix}jenkins/Jenkinsfile.deploy (main). Procedure: docs/GUIDE.md part F.")
        // Same parameters as the Jenkinsfile's parameters block (which replaces these after the first run), so the
        // very first run already offers "Build with Parameters" with TARGET restricted to this runway.
        parameters {
            choiceParam('TARGET', [id], 'Runway to deploy. Fixed by the folder of this job; other values are refused.')
            stringParam('SOURCE_JOB', "/${ciProjectPath}/${mainBranch}",
                'CI job whose bundle to deploy: the main job (main lane) or a branch job (branch lane).')
            stringParam('SOURCE_BUILD', '', 'Build number of SOURCE_JOB whose archived bundle is deployed.')
            stringParam('CHANGE_TICKET', '', 'Change ticket number. Required where runways.toml sets ticket_required.')
            booleanParam('DRY_RUN', false, 'Run every check, the dry-run report and the rehearsal, then stop.')
            booleanParam('ACK_DRIFT', false, 'Proceed although drift was found on a runway whose drift_gate is fail.')
            booleanParam('ALLOW_UNBASELINED', false, 'Only for a brand-new, empty database without Flyway history.')
        }
        definition {
            cpsScm {
                scm {
                    git {
                        remote {
                            url(repoUrl)
                            credentials(checkoutCredentialsId)
                        }
                        branch("*/${mainBranch}")
                    }
                }
                scriptPath(prefix + 'jenkins/Jenkinsfile.deploy')
                // Fetch only the Jenkinsfile to start the run; the pipeline checks out main itself (Resolve policy).
                lightweight(true)
            }
        }
    }
}
