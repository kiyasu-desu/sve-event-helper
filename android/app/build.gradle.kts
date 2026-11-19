import java.io.File
import java.net.URI
import java.util.Properties
import org.gradle.api.GradleException

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Default builds are public and embed NO server URL.
// Private debug builds must explicitly opt in with -PsveUsePrivateUrl=true,
// and release builds are unconditionally blocked from embedding private URLs.
val usePrivateUrl = project.findProperty("sveUsePrivateUrl")?.toString()?.toBoolean() == true
val hasReleaseTaskInStart = gradle.startParameter.taskNames.any {
    it.contains("release", ignoreCase = true)
}

if (usePrivateUrl && hasReleaseTaskInStart) {
    throw GradleException("Private server URLs must never be embedded in release builds (remove -PsveUsePrivateUrl=true).")
}

val debugBaseUrl = if (usePrivateUrl) {
    val privateConfig = Properties().apply {
        val file = rootProject.file("private.properties")
        if (file.isFile) file.inputStream().use { load(it) }
    }
    val candidate = (System.getenv("SVE_BASE_URL")
        ?: privateConfig.getProperty("sve.baseUrl") ?: "").trim()
    val uri = runCatching { URI(candidate) }.getOrNull()
    val host = uri?.host?.trimEnd('.')?.lowercase().orEmpty()
    require(
        uri != null && uri.scheme?.lowercase() == "https" &&
            host.isNotBlank() && uri.rawUserInfo == null &&
            uri.rawQuery == null && uri.rawFragment == null &&
            (uri.rawPath.isNullOrEmpty() || uri.rawPath == "/") &&
            host != "example.com" && !host.endsWith(".example.com")
    ) {
        "When -PsveUsePrivateUrl=true is set, set SVE_BASE_URL or android/private.properties sve.baseUrl to your existing HTTPS origin (no credentials/query/path)."
    }
    candidate.trimEnd('/')
} else {
    ""
}

// Release signing credentials stay outside Git via keystore.properties or env vars.
val keystoreConfig = Properties().apply {
    val file = rootProject.file("keystore.properties")
    if (file.isFile) file.inputStream().use { load(it) }
}
val rawStoreFile = (System.getenv("SVE_KEYSTORE_FILE")
    ?: keystoreConfig.getProperty("storeFile") ?: "").trim()
val releaseStorePassword = (System.getenv("SVE_KEYSTORE_PASSWORD")
    ?: keystoreConfig.getProperty("storePassword") ?: "").trim()
val releaseKeyAlias = (System.getenv("SVE_KEY_ALIAS")
    ?: keystoreConfig.getProperty("keyAlias") ?: "").trim()
val releaseKeyPassword = (System.getenv("SVE_KEY_PASSWORD")
    ?: keystoreConfig.getProperty("keyPassword") ?: "").trim()

val resolvedStoreFile: File? = rawStoreFile.takeIf { it.isNotEmpty() }?.let { path ->
    val direct = File(path)
    if (direct.isAbsolute) direct else rootProject.file(path)
}
val releaseSigningReady = resolvedStoreFile != null &&
    resolvedStoreFile.isFile &&
    releaseStorePassword.isNotEmpty() &&
    releaseStorePassword != "CHANGE_ME" &&
    releaseKeyAlias.isNotEmpty() &&
    releaseKeyPassword.isNotEmpty() &&
    releaseKeyPassword != "CHANGE_ME"

if (hasReleaseTaskInStart && !releaseSigningReady) {
    throw GradleException(
        "Release signing config is missing or incomplete. Copy android/keystore.properties.example to android/keystore.properties (or set SVE_KEYSTORE_FILE, SVE_KEYSTORE_PASSWORD, SVE_KEY_ALIAS, SVE_KEY_PASSWORD) and ensure the keystore file exists."
    )
}

gradle.taskGraph.whenReady {
    val runningRelease = allTasks.any {
        it.project == project && it.name.contains("Release", ignoreCase = false)
    }
    if (runningRelease && usePrivateUrl) {
        throw GradleException("Private server URLs must never be embedded in release builds (remove -PsveUsePrivateUrl=true).")
    }
    if (runningRelease && !releaseSigningReady) {
        throw GradleException(
            "Release signing config is missing or incomplete. Copy android/keystore.properties.example to android/keystore.properties (or set SVE_KEYSTORE_FILE, SVE_KEYSTORE_PASSWORD, SVE_KEY_ALIAS, SVE_KEY_PASSWORD) and ensure the keystore file exists."
        )
    }
}

android {
    namespace = "com.svetool.monitor"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.svetool.monitor"
        minSdk = 24
        targetSdk = 34
        // 版本号必须随每次构建递增：Android 靠 versionCode 判断"这是不是更新"，
        // 不变会让覆盖安装的行为不可预期；versionName 则显示给用户看。
        versionCode = 3
        versionName = "1.6"
        buildConfigField("String", "SVE_BASE_URL", "\"\"")
    }

    buildFeatures {
        // AGP 8 起 buildConfig 默认关闭；MainActivity 要读 BuildConfig.VERSION_*
        // 来把版本注入网页，因此必须显式打开。
        buildConfig = true
    }

    signingConfigs {
        if (releaseSigningReady) {
            create("release") {
                storeFile = resolvedStoreFile
                storePassword = releaseStorePassword
                keyAlias = releaseKeyAlias
                keyPassword = releaseKeyPassword
            }
        }
    }

    buildTypes {
        debug {
            isMinifyEnabled = false
            if (usePrivateUrl) {
                buildConfigField("String", "SVE_BASE_URL", "\"$debugBaseUrl\"")
            }
        }
        release {
            isMinifyEnabled = false
            // Unconditionally pin SVE_BASE_URL to empty string in release builds,
            // even under aggregate tasks like `gradle build`.
            buildConfigField("String", "SVE_BASE_URL", "\"\"")
            if (releaseSigningReady) {
                signingConfig = signingConfigs.getByName("release")
            }
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

dependencies {
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.webkit:webkit:1.12.1")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.activity:activity-ktx:1.9.3")

    testImplementation("junit:junit:4.13.2")
}
