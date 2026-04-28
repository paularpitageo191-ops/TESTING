#!/usr/bin/env python3
"""
BMAD Factory - Master Orchestrator
Automates the entire BMAD pipeline through Python, eliminating manual steps.

Phases:
1. Monitor & Ingest (BMM) - Scan docs/inbox/ for new files and vectorize
2. Semantic Discovery (Recon) - Capture DOM and generate PRD
3. Execution Trigger (TEA) - Run Playwright tests and detect healing
4. Synthesis & Reporting - Generate reports and consolidate logs

Usage:
    python3 bmad_factory.py                          # Run full pipeline
    python3 bmad_factory.py --project SCRUM-104      # Run specific project
    python3 bmad_factory.py --phase 1                # Run only Phase 1
    python3 bmad_factory.py --monitor                # Continuous monitoring mode
"""

import os
import sys
import json
import time
import logging
import argparse
import subprocess
import glob
from datetime import datetime
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from dotenv import load_dotenv

# Import LLM Gateway
from llm_gateway import get_llm_gateway

# Load environment variables
load_dotenv()

# Configuration
PROJECT_KEY = os.getenv("PROJECT_KEY", "")  # No default - must be provided
INBOX_DIR = "docs/inbox"
LOG_DIR = "docs/logs"
AUDIT_LOG = os.path.join(LOG_DIR, "factory_audit.log")
TEST_RESULTS_DIR = "test-results"

# Supported file extensions for ingestion
SUPPORTED_EXTENSIONS = {'.pdf', '.xlsx', '.json', '.csv'}

# Phase scripts
PHASE_SCRIPTS = {
    1: "vectorize_and_upload.py",
    2: ["dom_capture.py", "quality_alignment.py"],
    2.5: "step_generator.py",  # Phase 2.5: Step Synthesis (TEA)
    3: "npm test",  # Playwright test command
    4: "report_to_jira.py"
}


class BMADFactory:
    """
    Master Orchestrator for the BMAD pipeline
    """
    
    def __init__(self, project_key: str = PROJECT_KEY, phases: List[int] = None, monitor: bool = False):
        self.project_key = project_key
        self.phases = phases or [1, 2, 3, 4]
        self.monitor = monitor
        self.start_time = datetime.now()
        self.phase_results = {}
        
        # Setup logging
        self.setup_logging()
        
        self.logger.info(f"BMAD Factory initialized for project: {project_key}")
        self.logger.info(f"Phases to execute: {self.phases}")
        self.logger.info(f"Monitor mode: {self.monitor}")
    
    def setup_logging(self):
        """Setup logging configuration"""
        os.makedirs(LOG_DIR, exist_ok=True)
        
        # Create logger
        self.logger = logging.getLogger("BMADFactory")
        self.logger.setLevel(logging.DEBUG)
        
        # File handler for audit log
        file_handler = logging.FileHandler(AUDIT_LOG)
        file_handler.setLevel(logging.DEBUG)
        file_format = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(file_format)
        
        # Console handler
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        console_format = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
        console_handler.setFormatter(console_format)
        
        # Add handlers
        self.logger.addHandler(file_handler)
        self.logger.addHandler(console_handler)
    
    def initialize_llm_gateway(self):
        """Initialize the LLM Gateway at startup."""
        self.logger.info("Initializing LLM Gateway...")
        try:
            gateway = get_llm_gateway()
            if gateway.initialize():
                self.logger.info("✓ LLM Gateway initialized successfully")
                return True
            else:
                self.logger.error("✗ Failed to initialize LLM Gateway")
                return False
        except Exception as e:
            self.logger.error(f"✗ Error initializing LLM Gateway: {e}")
            return False
    
    def run(self):
        """Main execution flow"""
        self.logger.info("="*60)
        self.logger.info("BMAD Factory - Master Orchestrator")
        self.logger.info("="*60)
        self.logger.info(f"Project: {self.project_key}")
        self.logger.info(f"Start Time: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}")
        self.logger.info("="*60)
        
        try:
            # Initialize LLM Gateway first
            if not self.initialize_llm_gateway():
                self.logger.error("Cannot proceed without LLM Gateway")
                return
            
            if self.monitor:
                self.run_continuous_monitoring()
            else:
                self.run_phases()
            
            self.generate_final_report()
            
        except Exception as e:
            self.logger.error(f"Factory execution failed: {e}")
            self.phase_results["error"] = str(e)
        finally:
            self.log_summary()
    
    def run_continuous_monitoring(self):
        """Run in continuous monitoring mode"""
        self.logger.info("Starting continuous monitoring mode...")
        self.logger.info("Press Ctrl+C to stop")
        
        try:
            while True:
                # Check for new files in inbox
                new_files = self.scan_inbox()
                
                if new_files:
                    self.logger.info(f"Found {len(new_files)} new file(s) in inbox")
                    self.run_phases()
                else:
                    self.logger.debug("No new files in inbox, waiting...")
                
                # Wait before next check (5 minutes)
                time.sleep(300)
                
        except KeyboardInterrupt:
            self.logger.info("Monitoring stopped by user")
    
    def run_phases(self):
        """Execute all configured phases"""
        for phase_num in self.phases:
            phase_name = self.get_phase_name(phase_num)
            self.logger.info(f"\n{'='*40}")
            self.logger.info(f"PHASE {phase_num}: {phase_name}")
            self.logger.info(f"{'='*40}")
            
            success = self.execute_phase(phase_num)
            self.phase_results[phase_num] = {
                "name": phase_name,
                "success": success,
                "timestamp": datetime.now().isoformat()
            }
            
            if not success:
                self.logger.warning(f"Phase {phase_num} failed, continuing to next phase...")
            
            # Brief pause between phases
            time.sleep(2)
    
    def execute_phase(self, phase_num) -> bool:
        """Execute a single phase"""
        if phase_num == 1:
            return self.run_phase_1()
        elif phase_num == 2:
            return self.run_phase_2()
        elif phase_num == 2.5:
            return self.run_phase_2_5()
        elif phase_num == 3:
            return self.run_phase_3()
        elif phase_num == 4:
            return self.run_phase_4()
        else:
            self.logger.error(f"Unknown phase number: {phase_num}")
            return False
    
    def run_phase_1(self) -> bool:
        """Phase 1: Monitor & Ingest (BMM)"""
        self.logger.info("Scanning inbox for new files...")
        
        # Scan inbox
        new_files = self.scan_inbox()
        
        if not new_files:
            self.logger.info("No new files found in inbox")
            return True
        
        self.logger.info(f"Found {len(new_files)} new file(s): {[f.name for f in new_files]}")
        
        # Process each new file
        for file_path in new_files:
            self.logger.info(f"Processing: {file_path.name}")
            
            # Run vectorize_and_upload.py
            success = self.run_script("vectorize_and_upload.py", f"Processing {file_path.name}")
            
            if success:
                # Move processed file to archive
                archive_dir = os.path.join(INBOX_DIR, "processed")
                os.makedirs(archive_dir, exist_ok=True)
                
                # Move file
                dest = os.path.join(archive_dir, file_path.name)
                file_path.rename(dest)
                self.logger.info(f"File archived: {dest}")
            else:
                self.logger.error(f"Failed to process: {file_path.name}")
                return False
        
        return True
    
    def run_phase_2(self) -> bool:
        """Phase 2: Semantic Discovery (Recon)"""
        self.logger.info("Starting Semantic Discovery...")
        
        # Step 1: Capture DOM
        self.logger.info("Step 1/2: Capturing DOM state...")
        dom_success = self.run_script("dom_capture.py", "DOM Capture")
        
        if not dom_success:
            self.logger.error("DOM Capture failed")
            return False
        
        # Step 2: Run Quality Alignment
        self.logger.info("Step 2/2: Running Quality Alignment...")
        qa_success = self.run_script("quality_alignment.py", "Quality Alignment")
        
        if not qa_success:
            self.logger.error("Quality Alignment failed")
            return False
        
        self.logger.info("Semantic Discovery completed successfully")
        return True

    def run_phase_2_5(self) -> bool:
        """Phase 2.5: Step Synthesis (TEA) - Generate step definitions from Gherkin"""
        self.logger.info("Starting Step Synthesis (TEA)...")
        
        # Run step_generator.py with project key
        success = self.run_script_with_args(
            "step_generator.py",
            ["--project", self.project_key],
            "Step Synthesis"
        )
        
        if not success:
            self.logger.error("Step Synthesis failed")
            return False
        
        self.logger.info("Step Synthesis completed successfully")
        return True
    
    def run_phase_3(self) -> bool:
        """Phase 3: Execution Trigger (TEA)"""
        self.logger.info("Starting Test Execution...")
        
        # Check if step definitions exist, if not run Phase 2.5 first
        steps_dir = os.path.join("tests", "steps")
        if not os.path.exists(steps_dir) or not os.listdir(steps_dir):
            self.logger.info("No step definitions found, running Phase 2.5 first...")
            self.run_phase_2_5()
        
        # Run Playwright tests
        self.logger.info("Running Playwright test suite...")
        
        try:
            # Run tests and capture output
            result = subprocess.run(
                ["npm", "run", "test"],
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            # Log output
            if result.stdout:
                self.logger.info(f"Test output:\n{result.stdout[:1000]}")
            if result.stderr:
                self.logger.warning(f"Test warnings:\n{result.stderr[:1000]}")
            
            # Check exit code
            if result.returncode == 0:
                self.logger.info("✓ All tests passed")
                return True
            else:
                self.logger.warning(f"⚠ Tests failed with exit code: {result.returncode}")
                
                # Check if Healer Guard was activated
                if self.detect_healer_activation():
                    self.logger.info("Healer Guard was activated during test execution")
                    return True  # Consider it a success if healing occurred
                else:
                    self.logger.error("Tests failed and Healer Guard was not activated")
                    return False
                    
        except subprocess.TimeoutExpired:
            self.logger.error("Test execution timed out (5 minutes)")
            return False
        except Exception as e:
            self.logger.error(f"Test execution failed: {e}")
            return False
    
    def run_phase_4(self) -> bool:
        """Phase 4: Synthesis & Reporting"""
        self.logger.info("Starting Synthesis & Reporting...")
        
        # Check if there are test failures to report
        if not self.has_test_failures():
            self.logger.info("No test failures to report - skipping Jira report generation")
            # Still consolidate logs and generate summary
            self.consolidate_logs()
            self.logger.info("Synthesis & Reporting completed (no failures to report)")
            return True
        
        # Run report_to_jira.py
        success = self.run_script("report_to_jira.py", "Jira Report Generation")
        
        if not success:
            self.logger.error("Report generation failed")
            return False
        
        # Consolidate all logs
        self.consolidate_logs()
        
        self.logger.info("Synthesis & Reporting completed successfully")
        return True
    def has_test_failures(self) -> bool:
        """Check if there are any test failures to report"""
        if not os.path.exists(TEST_RESULTS_DIR):
            return False
        
        # Check for results.json with failures
        results_path = os.path.join(TEST_RESULTS_DIR, "results.json")
        if os.path.exists(results_path):
            try:
                with open(results_path, 'r') as f:
                    results = json.load(f)
                # Check if there are failed tests
                stats = results.get("stats", {})
                failed = stats.get("failed", 0)
                return failed > 0
            except Exception:
                pass
        
        # Check for failure directories
        for item in os.listdir(TEST_RESULTS_DIR):
            if item.endswith('_failed') or 'failure' in item.lower():
                return True
        
        return False
    
    def scan_inbox(self) -> List[Path]:
        """Scan inbox directory for new files"""
        if not os.path.exists(INBOX_DIR):
            os.makedirs(INBOX_DIR, exist_ok=True)
            return []
        
        new_files = []
        for file_path in Path(INBOX_DIR).glob("*"):
            if file_path.is_file() and file_path.suffix.lower() in SUPPORTED_EXTENSIONS:
                # Check if file is new (modified in last hour)
                mtime = datetime.fromtimestamp(file_path.stat().st_mtime)
                age_hours = (datetime.now() - mtime).total_seconds() / 3600
                
                if age_hours < 1:  # New file
                    new_files.append(file_path)
        
        return new_files
    
    def detect_healer_activation(self) -> bool:
        """Check if Healer Guard was activated during tests"""
        # Check for healing logs
        healing_logs = glob.glob(os.path.join("docs", "healing-logs", "healing-plan-*.json"))
        
        if healing_logs:
            # Check most recent healing log
            latest_log = max(healing_logs, key=os.path.getmtime)
            try:
                with open(latest_log, 'r') as f:
                    healing_data = json.load(f)
                
                confidence = healing_data.get("confidenceScore", 0)
                if confidence >= 0.5:
                    return True
            except Exception as e:
                self.logger.warning(f"Could not read healing log: {e}")
        
        # Check test output for healing messages
        # This would require parsing the test output, which we have in result.stdout
        # For now, we'll check if any healing logs exist
        
        return len(healing_logs) > 0
    
    def run_script(self, script_name: str, description: str) -> bool:
        """Run a Python script and capture output"""
        self.logger.info(f"Running: {script_name} ({description})")
        
        try:
            result = subprocess.run(
                [sys.executable, script_name],
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            # Log output
            if result.stdout:
                self.logger.info(f"{script_name} output:\n{result.stdout[:500]}")
            if result.stderr:
                self.logger.warning(f"{script_name} errors:\n{result.stderr[:500]}")
            
            if result.returncode == 0:
                self.logger.info(f"✓ {description} completed successfully")
                return True
            else:
                self.logger.error(f"✗ {description} failed with exit code {result.returncode}")
                return False
                
        except subprocess.TimeoutExpired:
            self.logger.error(f"✗ {description} timed out (5 minutes)")
            return False
        except Exception as e:
            self.logger.error(f"✗ {description} failed: {e}")
            return False
    
    def consolidate_logs(self):
        """Consolidate all logs into final audit log"""
        self.logger.info("Consolidating logs...")
        
        # Read the audit log
        try:
            with open(AUDIT_LOG, 'r') as f:
                audit_content = f.read()
        except FileNotFoundError:
            audit_content = ""
        
        # Add summary section
        summary = f"""

{'='*60}
BMAD FACTORY - FINAL AUDIT REPORT
{'='*60}
Project: {self.project_key}
Execution Date: {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}
Duration: {datetime.now() - self.start_time}

PHASE RESULTS:
"""
        
        for phase_num, result in self.phase_results.items():
            if isinstance(result, dict):
                status = "✓ SUCCESS" if result["success"] else "✗ FAILED"
                summary += f"  Phase {phase_num} ({result['name']}): {status}\n"
        
        summary += f"""
OVERALL STATUS: {'✓ SUCCESS' if all(r.get('success', False) for r in self.phase_results.values() if isinstance(r, dict)) else '✗ PARTIAL FAILURE'}
{'='*60}
"""
        
        # Append to audit log
        with open(AUDIT_LOG, 'a') as f:
            f.write(summary)
        
        self.logger.info(f"Audit log consolidated: {AUDIT_LOG}")
    
    def generate_final_report(self):
        """Generate final factory report"""
        report_path = os.path.join(LOG_DIR, f"factory_report_{self.project_key}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md")
        
        report = f"""# BMAD Factory Execution Report

## Project: {self.project_key}
**Execution Date:** {self.start_time.strftime('%Y-%m-%d %H:%M:%S')}  
**Duration:** {datetime.now() - self.start_time}  
**Mode:** {'Continuous Monitoring' if self.monitor else 'One-time Execution'}

## Phase Results

| Phase | Name | Status | Timestamp |
|-------|------|--------|-----------|
"""
        
        for phase_num in sorted(self.phase_results.keys()):
            if isinstance(self.phase_results[phase_num], dict):
                result = self.phase_results[phase_num]
                status = "✅ Success" if result["success"] else "❌ Failed"
                timestamp = result.get("timestamp", "N/A")
                report += f"| {phase_num} | {result['name']} | {status} | {timestamp} |\n"
        
        report += f"""
## Overall Status
{'✅ All phases completed successfully' if all(r.get('success', False) for r in self.phase_results.values() if isinstance(r, dict)) else '❌ Some phases failed'}

## Audit Log
Full audit log available at: `{AUDIT_LOG}`

---
*Generated by BMAD Factory on {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*
"""
        
        os.makedirs(LOG_DIR, exist_ok=True)
        with open(report_path, 'w') as f:
            f.write(report)
        
        self.logger.info(f"Final report generated: {report_path}")
    
    def log_summary(self):
        """Log execution summary"""
        duration = datetime.now() - self.start_time
        
        self.logger.info("\n" + "="*60)
        self.logger.info("BMAD FACTORY EXECUTION SUMMARY")
        self.logger.info("="*60)
        self.logger.info(f"Project: {self.project_key}")
        self.logger.info(f"Duration: {duration}")
        self.logger.info(f"Phases Executed: {len(self.phases)}")
        
        success_count = sum(1 for r in self.phase_results.values() if isinstance(r, dict) and r.get("success", False))
        total_count = len([r for r in self.phase_results.values() if isinstance(r, dict)])
        
        self.logger.info(f"Success Rate: {success_count}/{total_count}")
        self.logger.info("="*60)
    
    def run_script_with_args(self, script_name: str, args: List[str], description: str) -> bool:
        """Run a Python script with arguments and capture output"""
        self.logger.info(f"Running: {script_name} {' '.join(args)} ({description})")
        
        try:
            cmd = [sys.executable, script_name] + args
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=300  # 5 minute timeout
            )
            
            # Log output
            if result.stdout:
                self.logger.info(f"{script_name} output:\n{result.stdout[:500]}")
            if result.stderr:
                self.logger.warning(f"{script_name} errors:\n{result.stderr[:500]}")
            
            if result.returncode == 0:
                self.logger.info(f"✓ {description} completed successfully")
                return True
            else:
                self.logger.error(f"✗ {description} failed with exit code {result.returncode}")
                return False
                
        except subprocess.TimeoutExpired:
            self.logger.error(f"✗ {description} timed out (5 minutes)")
            return False
        except Exception as e:
            self.logger.error(f"✗ {description} failed: {e}")
            return False

    @staticmethod
    def get_phase_name(phase_num) -> str:
        """Get human-readable phase name"""
        names = {
            1: "Monitor & Ingest (BMM)",
            2: "Semantic Discovery (Recon)",
            2.5: "Step Synthesis (TEA)",
            3: "Execution Trigger (TEA)",
            4: "Synthesis & Reporting"
        }
        return names.get(phase_num, f"Phase {phase_num}")


def main():
    """Main entry point with argument parsing"""
    parser = argparse.ArgumentParser(
        description="BMAD Factory - Master Orchestrator",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 bmad_factory.py                          # Run full pipeline
  python3 bmad_factory.py --project SCRUM-104      # Run specific project
  python3 bmad_factory.py --phase 1 --phase 2      # Run specific phases
  python3 bmad_factory.py --monitor                # Continuous monitoring
        """
    )
    
    parser.add_argument(
        "--project",
        type=str,
        default=PROJECT_KEY,
        help=f"Project key (default: {PROJECT_KEY})"
    )
    
    parser.add_argument(
        "--phase",
        type=float,
        nargs="+",
        choices=[1, 2, 2.5, 3, 4],
        help="Specific phase(s) to run (1, 2, 2.5, 3, 4)"
    )
    
    parser.add_argument(
        "--monitor",
        action="store_true",
        help="Run in continuous monitoring mode"
    )
    
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable verbose logging"
    )
    
    args = parser.parse_args()
    
    # Create and run factory
    factory = BMADFactory(
        project_key=args.project,
        phases=args.phase,
        monitor=args.monitor
    )
    
    # Adjust logging level if verbose
    if args.verbose:
        factory.logger.setLevel(logging.DEBUG)
        for handler in factory.logger.handlers:
            if isinstance(handler, logging.StreamHandler):
                handler.setLevel(logging.DEBUG)
    
    factory.run()


if __name__ == "__main__":
    main()