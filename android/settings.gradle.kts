// 插件与依赖的仓库来源。国内网络下如果 gradlePluginPortal 慢，
// 可在 ~/.gradle/init.gradle 里换成镜像，不影响本文件。
pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "sve-monitor-android"
include(":app")
