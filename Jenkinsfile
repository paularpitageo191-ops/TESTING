pipeline {
    agent any

    environment {
        PROJECT_KEY = "SCRUM-70"
        PYTHON      = "python3"
        PIP         = "python3 -m pip"
        OLLAMA_HOST = "http://192.168.1.8:11434"
        QDRANT_URL  = "http://192.168.1.8:6333"
    }

    stages {

        // ── Stage 1: Setup — install Python deps inside workspace venv ───
        stage('Setup') {
            steps {
                sh '''
                    cd "$WORKSPACE"
        
                    # Python venv
                    if [ ! -f ".ws_venv/bin/activate" ]; then
                        python3 -m venv .ws_venv
                    fi
                    .ws_venv/bin/pip install --quiet --upgrade pip
                    .ws_venv/bin/pip install --quiet -r requirements.txt 2>/dev/null || \
                    .ws_venv/bin/pip install --quiet requests python-dotenv qdrant-client
                    .ws_venv/bin/python3 --version
        
                    # Node modules — needed for @playwright/test
                    npm install --silent
                '''
            }
        }
        // ── Stage 2: Bootstrap DB (idempotent) ───────────────────────────
        stage('Bootstrap') {
            steps {
                sh '''
                    cd "$WORKSPACE"
                    .ws_venv/bin/python3 db_init.py
                '''
            }
        }

        // ── Stage 3: Risk scoring + planning (UC-2) ──────────────────────
        stage('Risk scoring') {
            steps {
                sh '''
                    cd "$WORKSPACE"
                    .ws_venv/bin/python3 risk_scorer.py --project $PROJECT_KEY
                    .ws_venv/bin/python3 priority_planner.py \
                        --project $PROJECT_KEY \
                        --budget-minutes 20
                '''
            }
        }

        // ── Stage 4: Run Playwright tests ────────────────────────────────
        stage('Run tests') {
            steps {
                script {
                    def planContent = readFile("${WORKSPACE}/tests/steps/${PROJECT_KEY}_test_plan.json")
                    def plan        = new groovy.json.JsonSlurper().parseText(planContent)
                    def specFile    = "tests/steps/${PROJECT_KEY}.spec.ts"
        
                    // Pass grep pattern as a separate env var to avoid pipe interpretation
                    env.GREP_PATTERN = plan.grep_tags ? plan.grep_tags.join("|") : ""
                    env.SPEC_FILE    = specFile
                    env.RUN_ID       = "${PROJECT_KEY}-${currentBuild.number}"
                }
                sh '''
                    cd "$WORKSPACE"
                    export NVM_DIR="$HOME/.nvm"
                    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
                    NPX=$(which npx 2>/dev/null || echo "npx")
        
                    # Install chromium only (deps already installed)
                    $NPX playwright install chromium 2>/dev/null || true
        
                    # Run tests — grep pattern quoted so | is treated as regex OR not a pipe
                    if [ -n "$GREP_PATTERN" ]; then
                        $NPX playwright test "$SPEC_FILE" \
                            --grep "$GREP_PATTERN" \
                            --project=chromium \
                            --reporter=json > pw_report.json 2>&1 || true
                    else
                        $NPX playwright test "$SPEC_FILE" \
                            --project=chromium \
                            --reporter=json > pw_report.json 2>&1 || true
                    fi
        
                    echo "--- pw_report.json first 20 lines ---"
                    head -20 pw_report.json || echo "(empty)"
                '''
            }
        }
        // ── Stage 5: Analyse results (UC-3) ──────────────────────────────
        stage('Analyse results') {
            steps {
                sh '''
                    cd "$WORKSPACE"
                    # Check file exists AND starts with { (valid JSON)
                    if [ ! -f pw_report.json ] || ! head -c1 pw_report.json | grep -q '{'; then
                        echo "pw_report.json missing or not valid JSON — skipping analysis"
                        exit 0
                    fi
                    .ws_venv/bin/python3 result_analyzer.py \
                        --project $PROJECT_KEY \
                        --report  pw_report.json \
                        --run-id  "$RUN_ID"
                '''
            }
        }

        // ── Stage 6: Archive report ───────────────────────────────────────
        stage('Archive') {
            steps {
                archiveArtifacts artifacts: 'pw_report.json,tests/steps/*_risk_scores.json,tests/steps/*_test_plan.json',
                                 allowEmptyArchive: true
            }
        }
    }

    post {
        always {
            echo "Pipeline done — run_id: ${env.RUN_ID ?: 'not set'}"
        }
        success {
            echo "All stages passed."
        }
        failure {
            echo "Pipeline failed — check console above for stage that errored."
        }
    }
}
