plugins { java }

group = "com.riftx"
version = "2.0.0-alpha.0"

repositories { mavenCentral() }

dependencies {
    compileOnly("net.portswigger.burp.extensions:montoya-api:2025.2")
    testImplementation(platform("org.junit:junit-bom:5.12.2"))
    testImplementation("org.junit.jupiter:junit-jupiter")
}

java { toolchain { languageVersion.set(JavaLanguageVersion.of(21)) } }

tasks.test { useJUnitPlatform() }

tasks.jar {
    archiveBaseName.set("riftx-burp-extension")
    manifest { attributes["Main-Class"] = "com.riftx.burp.RiftXBurpExtension" }
}
