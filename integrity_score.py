"""
Integrity Score Calculator
Computes live integrity score with smooth transitions
"""

from typing import Dict, Optional


class IntegrityScoreCalculator:
    """Calculate and maintain integrity score (0-100)"""
    
    def __init__(self):
        self.current_score = 100.0
        self.decay_rate = 0.5  # Points per update for gradual drift
        self.shock_penalty = 15.0  # Points for sudden drift
        self.recovery_rate = 0.2  # Points per update when stable
        
    def calculate(
        self,
        drift_info: Dict,
        cause_info: Optional[Dict]
    ) -> float:
        """
        Calculate current integrity score based on drift and cause
        Score smoothly decays/recovers, never jumps randomly
        """
        
        if not drift_info["drifting"]:
            # Recovery: slowly increase score when stable
            self.current_score = min(100.0, self.current_score + self.recovery_rate)
        else:
            # Decay based on drift type and severity
            severity = drift_info["severity"]
            drift_type = drift_info["type"]
            
            if drift_type == "sudden":
                # Sharp drop for sudden drift
                penalty = self.shock_penalty * severity
                self.current_score = max(0.0, self.current_score - penalty)
            
            elif drift_type == "gradual":
                # Smooth decay for gradual drift
                penalty = self.decay_rate * severity
                self.current_score = max(0.0, self.current_score - penalty)
            
            # Additional penalty based on number of affected metrics
            num_affected = len(drift_info["metrics_affected"])
            if num_affected > 2:
                multi_metric_penalty = (num_affected - 2) * 0.3
                self.current_score = max(0.0, self.current_score - multi_metric_penalty)
            
            # Cause-based adjustment
            if cause_info:
                confidence = cause_info.get("confidence", 0.5)
                category = cause_info.get("category", "unknown")
                
                # Critical categories get higher penalties
                critical_categories = ["resource", "reliability", "degradation"]
                if category in critical_categories:
                    self.current_score = max(0.0, self.current_score - (confidence * 0.5))
        
        return round(self.current_score, 2)
    
    def get_state(self) -> str:
        """Get current system state based on score"""
        if self.current_score >= 80:
            return "STABLE"
        elif self.current_score >= 50:
            return "DRIFTING"
        else:
            return "CRITICAL"
