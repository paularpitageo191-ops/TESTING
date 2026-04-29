pipeline {
    agent any

    environment {
        PROJECT_KEY      = "SCRUM-70"
        PYTHON           = "python3"
        PIP              = "python3 -m pip"

        OLLAMA_HOST      = "http://192.168.1.8:11434"
        QDRANT_URL       = "http://192.168.1.8:6333"

        JIRA_BASE_URL    = "https://paularpitaseis.atlassian.net"
        JIRA_PROJECT_KEY = "SCRUM"
    }

    stages {

        // ─────────────────────────────────────────────
        stage('Setup') {
            steps {
                sh '''
                    cd "$WORKSPACE"

                    if [ ! -f .ws_venv/bin/activate ]; then
                        python3 -m venv .ws_venv
                    fi

                    .ws_venv/bin/pip install --quiet --upgrade pip
                    .ws_venv/bin/pip install --quiet -r requirements.txt

                    npm install --silent
                '''
            }
        }

        // ─────────────────────────────────────────────
        stage('Bootstrap DB') {
            steps {
                sh '''
                    cd "$WORKSPACE"
                    .ws_venv/bin/python3 db_init.py
                '''
            }
        }

        // ─────────────────────────────────────────────
        stage('Risk Scoring') {
            steps {
                sh '''
                    cd "$WORKSPACE"
                    .ws_venv/bin/python3 risk_scorer.py --project $PROJECT_KEY
                    .ws_venv/bin/python3 priority_planner.py --project $PROJECT_KEY --budget-minutes 20
                '''
            }
        }

        // ─────────────────────────────────────────────
        stage('Run Tests') {
            steps {
                script {
                    env.RUN_ID = "${env.PROJECT_KEY}-${env.BUILD_NUMBER}"
                }

                sh '''
                    cd "$WORKSPACE"

                    export NVM_DIR="$HOME/.nvm"
                    [ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"

                    npx playwright install chromium

                    npx playwright test tests/steps/${PROJECT_KEY}.spec.ts \
                        --project=chromium \
                        --reporter=json || true
                '''
            }
        }

        // ─────────────────────────────────────────────
        stage('Analyse Results') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'jira-creds',
                    usernameVariable: 'JIRA_EMAIL',
                    passwordVariable: 'JIRA_API_TOKEN'
                )]) {

                    sh '''
                        cd "$WORKSPACE"

                        .ws_venv/bin/python3 result_analyzer.py \
                            --project $PROJECT_KEY \
                            --report pw_report.json \
                            --run-id "$RUN_ID"
                    '''
                }
            }
        }

        // ─────────────────────────────────────────────
        stage('Classify Failures') {
            steps {
                withCredentials([usernamePassword(
                    credentialsId: 'jira-creds',
                    usernameVariable: 'JIRA_EMAIL',
                    passwordVariable: 'JIRA_API_TOKEN'
                )]) {

                    sh '''
                        cd "$WORKSPACE"

                        .ws_venv/bin/python3 classifier.py \
                            --project $PROJECT_KEY \
                            --run-id "$RUN_ID" \
                            --db failure_history.sqlite
                    '''
                }
            }
        }

        // ─────────────────────────────────────────────
        stage('Self-Heal Selectors') {
            steps {
                sh '''
                    cd "$WORKSPACE"

                    .ws_venv/bin/python3 selector_healer.py \
                        --project $PROJECT_KEY \
                        --db failure_history.sqlite
                '''
            }
        }

        // ─────────────────────────────────────────────
        stage('Validate Fixes') {
            steps {
                sh '''
                    cd "$WORKSPACE"

                    for spec in tests/steps/${PROJECT_KEY}*.spec.ts; do
                        .ws_venv/bin/python3 test_validator.py \
                            --project $PROJECT_KEY \
                            --spec "$spec" \
                            --db failure_history.sqlite \
                            --smoke-n 2
                    done
                '''
            }
        }

        // ─────────────────────────────────────────────
        stage('Archive') {
            steps {
                archiveArtifacts artifacts: '**/*.json', fingerprint: true
            }
        }
    }

    post {
        always {
            echo "Pipeline done — run_id: ${env.RUN_ID}"
        }
        success {
            echo "All stages passed."
        }
        failure {
            echo "Pipeline failed."
        }
    }
}
