import java.net.URI
import java.util.Properties

plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

// Private address stays outside Git. Environment takes precedence for CI builds.
val privateConfig = Properties().apply {
    val file = rootProject.file("private.properties")
    if (file.isFile) file.inputStream().use { load(it) }
}
val sveBaseUrl = (System.getenv("SVE_BASE_URL")
    ?: privateConfig.getProperty("sve.baseUrl") ?: "").trim()
val sveUri = runCatching { URI(sveBaseUrl) }.getOrNull()
require(sveUri != null && sveUri.scheme == "https" &&
    !sveUri.host.isNullOrBlank() && sveUri.rawUserInfo == null &&
    sveUri.rawQuery == null && sveUri.rawFragment == null &&
    (sveUri.rawPath.isNullOrEmpty() || sveUri.rawPath == "/") &&
    sveUri.host != "example.com" && !sveUri.host.endsWith(".example.com")) {
    "Set SVE_BASE_URL or android/private.properties sve.baseUrl to your existing HTTPS origin (no credentials/query/path)."
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
        // 界面上会显示 "App <versionName> (<versionCode>)"，方便确认装的是哪个构建。
        versionCode = 2
        versionName = "1.5"
        buildConfigField("String", "SVE_BASE_URL", "\"${sveBaseUrl.trimEnd('/')}\"")
    }

    buildFeatures {
        // AGP 8 起 buildConfig 默认关闭；MainActivity 要读 BuildConfig.VERSION_*
        // 来把版本注入网页，因此必须显式打开。
        buildConfig = true
    }

    buildTypes {
        // debug 签名即可侧载；不引入额外配置，保证"一条命令出包"。
        debug {
            isMinifyEnabled = false
        }
        release {
            isMinifyEnabled = false
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

    // 登录判定的逻辑测试。真机无法在此验证，所以把"什么时候可以提交"抽成
    // 纯函数（LoginGate）并用单元测试证明 —— 尤其是"陈旧 Cookie 永不提交"。
    testImplementation("junit:junit:4.13.2")
}
