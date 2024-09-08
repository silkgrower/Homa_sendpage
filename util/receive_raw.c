/* Copyright (c) 2019-2022 Homa Developers
 * SPDX-License-Identifier: BSD-1-Clause
 */

/* This is a test program that uses a raw socket to receive packets
 * on a given protocol and print their contents.
 *
 * Usage: receive_raw [protocol]
 */

#include <errno.h>
#include <netdb.h>
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <unistd.h>
#include <netinet/ip.h>
#include <sys/types.h>
#include <sys/socket.h>

#include "../homa.h"

int main(int argc, char** argv) {
	int fd;
	int protocol;
	ssize_t size;
#define BUF_SIZE 2000
	char buffer[BUF_SIZE];
	struct ip* ip_header = (struct ip *) buffer;
	int header_length;

	if (argc >= 2) {
		protocol = strtol(argv[1], NULL, 10);
		if (protocol == 0) {
			printf("Bad protocol number %s; must be integer\n",
					argv[3]);
			exit(1);
		}
	} else {
		protocol = IPPROTO_HOMA;
	}

	fd = socket(AF_INET, SOCK_RAW, protocol);
	if (fd < 0) {
		printf("Couldn't open raw socket: %s\n", strerror(errno));
		exit(1);
	}

	while (1) {
		size = recvfrom(fd, buffer, BUF_SIZE, 0,  NULL, 0);
		if (size < 0) {
			printf("Error receiving packet: %s\n", strerror(errno));
			exit(1);
		}
		header_length = 4 * ip_header->ip_hl;
		// printf("IP header length: %d bytes\n", header_length);
		buffer[size] = 0;
		printf("%s\n", buffer + header_length);
	}
}
