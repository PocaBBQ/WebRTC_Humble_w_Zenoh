from setuptools import setup, find_packages
from pathlib import Path

package_name = 'ros_streamer'
here = Path(__file__).parent
reqs = (here / 'requirements.txt').read_text().splitlines()

setup(
    name=package_name,
    version='0.1.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages', ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/launch', ['launch/ros_streamer.launch.py']),
        ('share/' + package_name + '/static', ['ros_streamer/static/index.html']),
    ],
    install_requires=reqs,
    zip_safe=True,
    author='you',
    author_email='you@example.com',
    description='ROS 2 node that streams a V4L2 camera via WebRTC (FastAPI + aiortc).',
    license='MIT',
    entry_points={
        'console_scripts': [
            'ros_streamer_server = ros_streamer.server_node:main',
        ],
    },
)
