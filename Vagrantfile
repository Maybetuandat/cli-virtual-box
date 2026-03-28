Vagrant.configure("2") do |config|
  config.vm.box = "ubuntu/jammy64"

  config.vm.provider "virtualbox" do |vb|
    # Chế độ không giao diện
    vb.gui = false
    vb.memory = "2048"
    vb.cpus = 2

    # QUAN TRỌNG: Tắt USB vì không có Extension Pack
    vb.customize ["modifyvm", :id, "--usb", "off"]
    vb.customize ["modifyvm", :id, "--usbehci", "off"]

    
    vb.customize ["modifyvm", :id, "--paravirtprovider", "default"]
  end
end