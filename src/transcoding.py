"""
Transcoding Profile System

This module provides transcoding profiles similar for m3u-proxy
with template-based parameter substitution and hardware acceleration.
"""

import re
import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from hwaccel import hw_accel, get_ffmpeg_hwaccel_args

logger = logging.getLogger(__name__)


@dataclass
class TranscodingProfile:
    """A transcoding profile with template variables."""
    name: str
    parameters: str
    description: Optional[str] = None
    
    def render(self, variables: Dict[str, Any]) -> List[str]:
        """
        Render the profile parameters with provided variables.
        
        Args:
            variables: Dictionary of template variables to substitute
            
        Returns:
            List of FFmpeg arguments
        """
        # Start with the template parameters
        rendered_params = self.parameters
        
        # Replace template variables with default value support
        import re
        
        # First, handle variables with default values: {key|default}
        def replace_with_default(match):
            full_match = match.group(0)  # Full {key|default} or {key}
            var_name = match.group(1)    # The variable name
            default_value = match.group(2)  # The default value (if any)
            
            if var_name in variables:
                return str(variables[var_name])
            elif default_value is not None:
                return default_value
            else:
                # Keep the placeholder if no value and no default
                return full_match
        
        # Pattern to match {variable} or {variable|default}
        pattern = r'\{([^}|]+)(?:\|([^}]*))?\}'
        rendered_params = re.sub(pattern, replace_with_default, rendered_params)
        
        # Split into arguments (respecting quotes)
        args = []
        current_arg = ""
        in_quotes = False
        quote_char = None
        
        i = 0
        while i < len(rendered_params):
            char = rendered_params[i]
            
            if char in ['"', "'"] and not in_quotes:
                in_quotes = True
                quote_char = char
            elif char == quote_char and in_quotes:
                in_quotes = False
                quote_char = None
            elif char == " " and not in_quotes:
                if current_arg.strip():
                    args.append(current_arg.strip())
                current_arg = ""
                i += 1
                continue
            
            current_arg += char
            i += 1
        
        # Add the final argument if any
        if current_arg.strip():
            args.append(current_arg.strip())
        
        return args


class TranscodingProfileManager:
    """Manages transcoding profiles and provides default profiles."""
    
    def __init__(self):
        self.profiles = self._create_default_profiles()
    
    def _create_default_profiles(self) -> Dict[str, TranscodingProfile]:
        """Create default transcoding profiles with hardware acceleration support."""
        profiles = {}
        
        # Get hardware acceleration arguments
        hwaccel_args = hw_accel.get_basic_args()
        hwaccel_str = " ".join(hwaccel_args) + " " if hwaccel_args else ""
        
        # Determine the best encoder based on available hardware
        if hw_accel.is_available():
            if hw_accel.get_type() == "nvidia":
                h264_encoder = "h264_nvenc"
                h265_encoder = "hevc_nvenc"
                preset = "fast"
            elif hw_accel.get_type() in ["intel", "amd", "vaapi"]:
                h264_encoder = "h264_vaapi"
                h265_encoder = "hevc_vaapi" 
                preset = "medium"
            else:
                h264_encoder = "libx264"
                h265_encoder = "libx265"
                preset = "faster"
        else:
            h264_encoder = "libx264"
            h265_encoder = "libx265"
            preset = "faster"
        
        # Default profile - basic transcoding with hardware acceleration
        profiles["default"] = TranscodingProfile(
            name="default",
            description="Default profile with hardware acceleration",
            parameters=f"{hwaccel_str}-i {{input_url}} -c:v {h264_encoder} -preset {preset} -b:v {{video_bitrate|2M}} -c:a aac -b:a {{audio_bitrate|128k}} -f {{format|mp4}} {{output_args|}}"
        )
        
        # High quality profile
        profiles["hq"] = TranscodingProfile(
            name="hq",
            description="High quality transcoding",
            parameters=f"{hwaccel_str}-i {{input_url}} -c:v {h264_encoder} -preset {preset} -crf {{crf|23}} -c:a aac -b:a {{audio_bitrate|192k}} -f {{format|mp4}} {{output_args|}}"
        )
        
        # Low latency profile
        profiles["lowlatency"] = TranscodingProfile(
            name="lowlatency", 
            description="Low latency streaming",
            parameters=f"{hwaccel_str}-i {{input_url}} -c:v {h264_encoder} -preset {preset} -tune zerolatency -b:v {{video_bitrate|1M}} -c:a aac -b:a {{audio_bitrate|96k}} -f {{format|mpegts}} {{output_args|}}"
        )
        
        # 720p profile
        profiles["720p"] = TranscodingProfile(
            name="720p",
            description="720p transcoding", 
            parameters=f"{hwaccel_str}-i {{input_url}} -c:v {h264_encoder} -preset {preset} -vf scale=1280:720 -b:v {{video_bitrate|2500k}} -c:a aac -b:a {{audio_bitrate|128k}} -f {{format|mp4}} {{output_args|}}"
        )
        
        # 1080p profile
        profiles["1080p"] = TranscodingProfile(
            name="1080p",
            description="1080p transcoding",
            parameters=f"{hwaccel_str}-i {{input_url}} -c:v {h264_encoder} -preset {preset} -vf scale=1920:1080 -b:v {{video_bitrate|5000k}} -c:a aac -b:a {{audio_bitrate|192k}} -f {{format|mp4}} {{output_args|}}"
        )
        
        # HEVC/H.265 profile
        profiles["hevc"] = TranscodingProfile(
            name="hevc",
            description="HEVC/H.265 encoding",
            parameters=f"{hwaccel_str}-i {{input_url}} -c:v {h265_encoder} -preset {preset} -b:v {{video_bitrate|2M}} -c:a aac -b:a {{audio_bitrate|128k}} -f {{format|mp4}} {{output_args|}}"
        )
        
        # Audio only profile
        profiles["audio"] = TranscodingProfile(
            name="audio",
            description="Audio only transcoding",
            parameters=f"-i {{input_url}} -vn -c:a aac -b:a {{audio_bitrate|192k}} -f {{format|adts}} {{output_args|}}"
        )
        
        return profiles
    
    def get_profile(self, name: str) -> Optional[TranscodingProfile]:
        """Get a profile by name."""
        return self.profiles.get(name)
    
    def list_profiles(self) -> Dict[str, str]:
        """List all available profiles with descriptions."""
        return {name: profile.description or profile.name for name, profile in self.profiles.items()}
    
    def create_profile_from_template(self, name: str, parameters: str, description: Optional[str] = None) -> TranscodingProfile:
        """
        Create a custom profile from a parameter template.
        
        Args:
            name: Profile name
            parameters: FFmpeg parameter template with variables
            description: Optional description
            
        Returns:
            TranscodingProfile instance
        """
        return TranscodingProfile(name=name, parameters=parameters, description=description)
    
    def get_default_variables(self) -> Dict[str, str]:
        """Get default template variables."""
        return {
            "input_url": "",  # Will be filled by the API
            "output_args": "",  # Will be filled by the API (e.g., pipe output)
            "video_bitrate": "2M",
            "audio_bitrate": "128k", 
            "format": "mp4",
            "crf": "23",
        }
    
    def validate_template(self, parameters: str) -> bool:
        """
        Validate a parameter template for common issues.
        
        Args:
            parameters: Parameter template string
            
        Returns:
            True if template appears valid, False otherwise
        """
        # Check for required input placeholder
        if "{input_url}" not in parameters:
            logger.warning("Template missing {input_url} placeholder")
            return False
        
        # Check for balanced braces
        open_braces = parameters.count("{")
        close_braces = parameters.count("}")
        if open_braces != close_braces:
            logger.warning("Template has unbalanced braces")
            return False
        
        # Check for dangerous commands (basic security)
        dangerous_patterns = [
            r'\brm\b', r'\bdel\b', r'\bmv\b', r'\bcp\b',
            r'\bwget\b', r'\bcurl\b', r'\bsh\b', r'\bbash\b',
            r'\b;\b', r'\b&&\b', r'\b\|\|\b', r'\b`\b'
        ]
        
        for pattern in dangerous_patterns:
            if re.search(pattern, parameters, re.IGNORECASE):
                logger.warning(f"Template contains potentially dangerous pattern: {pattern}")
                return False
        
        return True


# Global profile manager instance
profile_manager = TranscodingProfileManager()


def get_profile_manager() -> TranscodingProfileManager:
    """Get the global profile manager instance."""
    return profile_manager