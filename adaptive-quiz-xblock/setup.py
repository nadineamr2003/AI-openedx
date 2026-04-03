from setuptools import setup, find_packages

setup(
    name="adaptive-quiz-xblock",
    version="0.1.0",
    description="AI-Powered Adaptive Quiz XBlock for Open edX",
    packages=find_packages(),
    install_requires=[
        "XBlock",
        "requests",
    ],
    entry_points={
        "xblock.v1": [
            "adaptivequiz = quiz.quiz:AdaptiveQuizXBlock",
        ],
    },
    package_data={
        "quiz": [
            "static/html/*.html",
            "static/js/*.js",
            "static/css/*.css",
        ],
    },
)