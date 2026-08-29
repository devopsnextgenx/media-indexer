-- Create the user allowed to connect from any remote host '%'
CREATE USER 'minis'@'%' IDENTIFIED BY 'm1nIspsswd';

CREATE DATABASE IF NOT EXISTS medialib;

-- Grant all privileges on the medialib database
GRANT ALL PRIVILEGES ON medialib.* TO 'minis'@'%';

-- Apply the privilege changes
FLUSH PRIVILEGES;