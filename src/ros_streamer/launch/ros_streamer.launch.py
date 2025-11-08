from launch import LaunchDescription
from launch_ros.actions import Node
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration

def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument('device', default_value='/dev/video0'),
        DeclareLaunchArgument('width', default_value='1280'),
        DeclareLaunchArgument('height', default_value='720'),
        DeclareLaunchArgument('fps', default_value='30'),
        DeclareLaunchArgument('host', default_value='0.0.0.0'),
        DeclareLaunchArgument('port', default_value='8000'),
        DeclareLaunchArgument('stun_urls', default_value=''),

        Node(
            package='ros_streamer',
            executable='ros_streamer_server',
            name='ros_streamer_server',
            output='screen',
            parameters=[{
                            'device': LaunchConfiguration('device'),
                            'width': LaunchConfiguration('width'),
                            'height': LaunchConfiguration('height'),
                            'fps': LaunchConfiguration('fps'),
                            'host': LaunchConfiguration('host'),
                            'port': LaunchConfiguration('port'),
                            'stun_urls': LaunchConfiguration('stun_urls'),  # string -> parsed in node
                        }]
        )
    ])
