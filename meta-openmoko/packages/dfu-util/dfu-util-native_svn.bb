require dfu-util_${PV}.bb

inherit native

DEPENDS = "libusb-native usbpath-native"

do_deploy() {
	install -d ${DEPLOY_DIR_IMAGE}
	install -m 0755 src/dfu-util_static ${DEPLOY_DIR_IMAGE}/dfu-util
}

addtask deploy before do_package after do_install
