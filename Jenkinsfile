pipeline {
    agent any

    environment {
        PROJECT_KEY = "SCRUM-70"
    }

    stages {

        // ── Stage 1: Bootstrap DB (idempotent) ───────────────────────────
        stage('Bootstrap') {
            steps {
                sh '''
                    cd "$WORKSPACE"
                    .venv/bin/python3 db_init.py
                '''
            }
        }

        // ── Stage 2: Score + plan (UC-2) ─────────────────────────────────
        stage('Risk scoring') {
            steps {
                sh '''
                    cd "$WORKSPACE"
                    .venv/bin/python3 risk_scorer.py --project $PROJECT_KEY
                    .venv/bin/python3 priority_planner.py --project $PROJECT_KEY --budget-minutes 20
                '''
            }
        }

        // ── Stage 3: Run Playwright tests ────────────────────────────────
        stage('Run tests') {
            steps {
                script {
                    def planPath = "${WORKSPACE}/tests/steps/${PROJECT_KEY}_test_plan.json"
                    def plan     = readJSON file: planPath
                    def specFile = "tests/steps/${PROJECT_KEY}.spec.ts"

                    def grepArg = ""
                    if (plan.grep_tags && plan.grep_tags.size() > 0) {
                        def pattern = plan.grep_tags.join("|")
                        grepArg = "--grep \"${pattern}\""
                    }

                    env.PW_COMMAND = "npx playwright test ${specFile} ${grepArg} --project=chromium --reporter=json"
                    env.RUN_ID     = "${PROJECT_KEY}-${currentBuild.number}-${currentBuild.startTimeInMillis}"
                }
                sh '''
                    cd "$WORKSPACE"
                    export NVM_DIR="$HOME/.nvm"
                    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
                    eval "$PW_COMMAND" > pw_report.json 2>&1 || true
                '''
            }
        }

        // ── Stage 4: Analyse results (UC-3) ──────────────────────────────
        stage('Analyse results') {
            steps {
                sh '''
                    cd "$WORKSPACE"
                    .venv/bin/python3 result_analyzer.py \
                        --project $PROJECT_KEY \
                        --report  pw_report.json \
                        --run-id  "$RUN_ID"
                '''
            }
        }

        // ── Stage 5: Archive report ───────────────────────────────────────
        stage('Archive') {
            steps {
                archiveArtifacts artifacts: 'pw_report.json', allowEmptyArchive: true
            }
        }
    }

    post {
        always {
            echo "Pipeline done — run_id: ${env.RUN_ID}"
        }
    }
}
