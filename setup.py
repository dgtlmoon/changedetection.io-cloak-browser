#!/usr/bin/env python
from setuptools import setup, find_packages
import os

here = os.path.abspath(os.path.dirname(__file__))

with open(os.path.join(here, 'README.md'), encoding='utf-8') as f:
    long_description = f.read()

setup(
    name='changedetection.io-cloak-browser',
    version='0.1.0',
    description='CloakBrowser stealth fetcher plugin for changedetection.io (anti-bot bypass)',
    long_description=long_description,
    long_description_content_type='text/markdown',
    author='dgtlmoon',
    author_email='dgtlmoon@gmail.com',
    url='https://github.com/dgtlmoon/changedetection.io-cloak-browser',
    packages=find_packages(),
    include_package_data=True,
    package_data={
        'changedetection_cloak_browser': ['static/*'],
    },
    install_requires=[
        'changedetection.io>=0.54.6',
        'cloakbrowser>=0.3.0',
        'playwright>=1.40.0',
    ],
    # Register as a changedetectionio plugin via entry_points
    entry_points={
        'changedetectionio': [
            'cloak_browser = changedetection_cloak_browser.fetcher',
        ],
    },
    python_requires='>=3.10',
    classifiers=[
        'Development Status :: 4 - Beta',
        'Intended Audience :: Developers',
        'Intended Audience :: System Administrators',
        'License :: OSI Approved :: MIT License',
        'Programming Language :: Python :: 3',
        'Programming Language :: Python :: 3.10',
        'Programming Language :: Python :: 3.11',
        'Programming Language :: Python :: 3.12',
        'Topic :: Internet :: WWW/HTTP :: Site Management',
        'Topic :: System :: Monitoring',
        'Topic :: Security',
    ],
    keywords='changedetection web monitoring anti-bot stealth chromium cloakbrowser cloudflare bypass',
)
