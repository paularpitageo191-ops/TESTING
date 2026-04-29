pipeline {
    agent any

    environment {
        PROJECT_KEY = "SCRUM-70"
        PYTHON      = "python3"
        PIP         = "python3 -m pip"
        PROJECT_KEY = "SCRUM-70"
        OLLAMA_HOST = "http://192.168.1.8:11434"
    }

    stages {

        // ── Stage 1: Setup — install Python deps inside workspace venv ───
        stage('Setup') {
            steps {
                sh '''
                    cd "$WORKSPACE"

                    # Create venv inside Jenkins workspace if not cached
                    if [ ! -f ".ws_venv/bin/activate" ]; then
                        $PYTHON -m venv .ws_venv
                    fi

                    # Install requirements
                    .ws_venv/bin/pip install --quiet --upgrade pip
                    .ws_venv/bin/pip install --quiet -r requirements.txt 2>/dev/null || \
                    .ws_venv/bin/pip install --quiet \
                        requests python-dotenv qdrant-client

                    # Confirm python works
                    .ws_venv/bin/python3 --version
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

                    def grepArg = ""
                    if (plan.grep_tags && plan.grep_tags.size() > 0) {
                        grepArg = "--grep \"${plan.grep_tags.join('|')}\""
                    }

                    env.PW_COMMAND = "npx playwright test ${specFile} ${grepArg} --project=chromium --reporter=json"
                    env.RUN_ID     = "${PROJECT_KEY}-${currentBuild.number}"
                }
                sh '''
                    cd "$WORKSPACE"
                    export NVM_DIR="$HOME/.nvm"
                    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
                    NPX=$(which npx 2>/dev/null \
                          || ls /usr/local/bin/npx 2>/dev/null \
                          || ls /usr/bin/npx 2>/dev/null \
                          || echo "npx")
                    $NPX playwright install chromium --with-deps 2>/dev/null || true
                    eval "$PW_COMMAND" > pw_report.json 2>&1 || true
                    echo "--- playwright output ---"
                    cat pw_report.json | head -50
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
